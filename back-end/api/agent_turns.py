"""Durable, server-driven continuation of dataset workflows."""

import logging
from datetime import timedelta

from django.conf import settings
from django.db import close_old_connections, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from api.models import Agent, AgentTurnJob, Dataset, Message


logger = logging.getLogger(__name__)


def _available_at(last_message):
    poll = Agent._gbif_poll_status(last_message)
    parsed = parse_datetime(poll.get('next_recheck_at', '')) if poll else None
    if parsed and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return max(parsed, timezone.now()) if parsed else timezone.now()


def queue_agent_turn(agent, delay_seconds=0):
    """Ensure a resumable turn exists without starting work in the caller."""
    last_message = agent.message_set.last()
    if not last_message or agent.completed_at or last_message.role == Message.Role.ASSISTANT:
        return None
    available_at = max(
        _available_at(last_message),
        timezone.now() + timedelta(seconds=delay_seconds),
    )
    with transaction.atomic():
        job, created = AgentTurnJob.objects.select_for_update().get_or_create(
            agent=agent,
            defaults={
                'requested_after_message_id': last_message.id,
                'available_at': available_at,
            },
        )
        if not created and job.claimed_at is None and job.requested_after_message_id != last_message.id:
            job.requested_after_message_id = last_message.id
            job.available_at = available_at
            job.failure_count = 0
            job.save(update_fields=['requested_after_message_id', 'available_at', 'failure_count'])
    return job


def ensure_dataset_work(dataset_id, delay_seconds=0):
    """Start or resume a dataset after a committed upload or user message."""
    with transaction.atomic():
        dataset = Dataset.objects.select_for_update().get(pk=dataset_id)
        agent = dataset.agent_set.filter(completed_at__isnull=True).first()
        if not agent:
            if dataset.published_at or (
                not dataset.user_files.exists() and not dataset.table_set.exists()
            ):
                return None
            previous = dataset.agent_set.last()
            # A worker may still be finishing the completion tool's turn.
            if previous and AgentTurnJob.objects.filter(agent=previous).exists():
                return None
            agent = dataset.next_agent()
        return queue_agent_turn(agent, delay_seconds=delay_seconds) if agent else None


def _has_incomplete_tool_calls(agent):
    assistant = agent.message_set.filter(openai_obj__has_key='tool_calls').last()
    if not assistant:
        return False
    calls = (assistant.openai_obj or {}).get('tool_calls') or []
    expected = {call.get('id') for call in calls if isinstance(call, dict)}
    if not expected:
        return False
    recorded = set(
        agent.message_set.filter(id__gt=assistant.id, openai_obj__role=Message.Role.TOOL)
        .values_list('openai_obj__tool_call_id', flat=True)
    )
    return bool(expected - recorded)


def _finish_claimed_job(job_id, claimed_at):
    """Atomically retain a continuation or retire a job at a pause boundary."""
    with transaction.atomic():
        # Requests acquire the dataset before the job. Use that same lock order
        # when a completed task advances, so a simultaneous reply cannot deadlock.
        job_probe = AgentTurnJob.objects.select_related('agent').filter(pk=job_id).first()
        if not job_probe:
            return
        dataset = Dataset.objects.select_for_update().get(pk=job_probe.agent.dataset_id)
        job = AgentTurnJob.objects.select_for_update().filter(pk=job_id, claimed_at=claimed_at).first()
        if not job:
            return
        agent = Agent.objects.get(pk=job.agent_id)
        if _has_incomplete_tool_calls(agent):
            Agent.objects.filter(pk=agent.pk).update(busy_thinking=False)
            Message.objects.create(agent=agent, openai_obj={
                'role': Message.Role.ASSISTANT,
                'content': (
                    'Processing stopped while a tool action was interrupted. '
                    'Please contact support so the action can be checked before retrying.'
                ),
                'workflow_error': True,
            })
            job.delete()
            return
        if agent.completed_at:
            next_agent = dataset.next_agent()
            if next_agent:
                last_message = next_agent.message_set.last()
                if AgentTurnJob.objects.filter(agent=next_agent).exists():
                    job.delete()
                    return
                job.agent = next_agent
                job.requested_after_message_id = last_message.id
                job.available_at = _available_at(last_message)
                job.claimed_at = None
                job.failure_count = 0
                job.save(update_fields=[
                    'agent', 'requested_after_message_id', 'available_at',
                    'claimed_at', 'failure_count',
                ])
                return
            job.delete()
            return

        last_message = agent.message_set.last()
        if not last_message or last_message.role == Message.Role.ASSISTANT:
            job.delete()
            return
        available_at = _available_at(last_message)
        if last_message.id == job.requested_after_message_id:
            # The turn made no progress (e.g. another process holds busy_thinking),
            # so wait rather than reclaiming the job in a tight loop.
            idle_seconds = getattr(settings, 'AGENT_TURN_IDLE_RETRY_SECONDS', 30)
            available_at = max(available_at, timezone.now() + timedelta(seconds=idle_seconds))
        job.requested_after_message_id = last_message.id
        job.available_at = available_at
        job.claimed_at = None
        job.failure_count = 0
        job.save(update_fields=[
            'requested_after_message_id', 'available_at', 'claimed_at', 'failure_count',
        ])


def _retry_or_fail_job(job_id, claimed_at):
    with transaction.atomic():
        job = AgentTurnJob.objects.select_for_update().filter(pk=job_id, claimed_at=claimed_at).first()
        if not job:
            return
        job.failure_count += 1
        if job.failure_count >= 3:
            Agent.objects.filter(pk=job.agent_id).update(busy_thinking=False)
            Message.objects.create(agent_id=job.agent_id, openai_obj={
                'role': Message.Role.ASSISTANT,
                'content': 'Processing stopped because of a server error. Please contact support or try again later.',
                'workflow_error': True,
            })
            job.delete()
            return
        job.claimed_at = None
        job.available_at = timezone.now() + timedelta(seconds=min(60 * 2 ** job.failure_count, 600))
        job.save(update_fields=['claimed_at', 'available_at', 'failure_count'])


def process_next_agent_turn():
    """Claim one due turn. The job survives each turn until a genuine pause."""
    close_old_connections()
    now = timezone.now()
    stale_before = now - timedelta(seconds=getattr(settings, 'AGENT_TURN_LEASE_SECONDS', 3600))
    with transaction.atomic():
        job = (
            AgentTurnJob.objects.select_for_update(skip_locked=True)
            .filter(available_at__lte=now)
            .filter(Q(claimed_at__isnull=True) | Q(claimed_at__lt=stale_before))
            .order_by('available_at', 'requested_at', 'id')
            .first()
        )
        if not job:
            return False
        was_stale = job.claimed_at is not None
        job.claimed_at = now
        job.save(update_fields=['claimed_at'])
        if was_stale:
            Agent.objects.filter(pk=job.agent_id).update(busy_thinking=False)

    try:
        agent = Agent.objects.get(pk=job.agent_id)
        last_message = agent.message_set.last()
        if not agent.completed_at and last_message and last_message.id == job.requested_after_message_id:
            agent.next_message()
        _finish_claimed_job(job.pk, now)
    except Exception:
        logger.exception('Agent turn worker failed for agent %s', job.agent_id)
        _retry_or_fail_job(job.pk, now)
        raise
    finally:
        close_old_connections()
    return True


def reconcile_recent_work(days=2):
    """Recover recently active work that predates server-driven continuation."""
    cutoff = timezone.now() - timedelta(days=days)
    dataset_ids = set(
        Message.objects.filter(created_at__gte=cutoff)
        .values_list('agent__dataset_id', flat=True)
        .distinct()
    )
    dataset_ids.update(
        Agent.objects.filter(completed_at__gte=cutoff)
        .values_list('dataset_id', flat=True)
        .distinct()
    )
    for dataset_id in sorted(dataset_ids):
        try:
            ensure_dataset_work(dataset_id)
        except Dataset.DoesNotExist:
            continue
        except Exception:
            logger.exception('Could not reconcile dataset %s', dataset_id)
