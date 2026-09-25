"""Run agent turns outside HTTP requests so Flex latency cannot stall refresh."""

import logging
from datetime import timedelta

from django.conf import settings
from django.db import close_old_connections, transaction
from django.db.models import Q
from django.utils import timezone

from api.models import Agent, AgentTurnJob, Message


logger = logging.getLogger(__name__)


def queue_agent_turn(agent):
    last_message = agent.message_set.last()
    if not last_message or agent.completed_at or agent.busy_thinking or last_message.role == Message.Role.ASSISTANT:
        return None
    job, _ = AgentTurnJob.objects.get_or_create(
        agent=agent,
        defaults={'requested_after_message_id': last_message.id},
    )
    return job


def process_next_agent_turn():
    """Claim one queued turn; a long lease permits recovery after worker crashes."""
    close_old_connections()
    now = timezone.now()
    stale_before = now - timedelta(seconds=getattr(settings, 'AGENT_TURN_LEASE_SECONDS', 3600))
    with transaction.atomic():
        job = (
            AgentTurnJob.objects.select_for_update(skip_locked=True)
            .filter(Q(claimed_at__isnull=True) | Q(claimed_at__lt=stale_before))
            .order_by('requested_at', 'id')
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
    except Exception:
        logger.exception('Agent turn worker failed for agent %s', job.agent_id)
        raise
    finally:
        AgentTurnJob.objects.filter(pk=job.pk, claimed_at=now).delete()
        close_old_connections()
    return True
