import logging
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.db import models
from django.db.models import Q
from django.utils import timezone

from api.models import DatasetAttentionNotification, Message


logger = logging.getLogger(__name__)


def attention_kind_for_dataset(dataset):
    if dataset.published_at:
        return DatasetAttentionNotification.AttentionKind.PUBLISHED
    if dataset.package_ready:
        return DatasetAttentionNotification.AttentionKind.READY

    active_agent = dataset.agent_set.filter(completed_at__isnull=True).order_by('created_at').first()
    if not active_agent or active_agent.busy_thinking:
        return None

    last_message = active_agent.message_set.order_by('-created_at').first()
    if not last_message or last_message.role != Message.Role.ASSISTANT:
        return None
    if (last_message.openai_obj or {}).get('tool_calls'):
        return None
    return DatasetAttentionNotification.AttentionKind.NEEDS_INPUT


def mark_notification_ready_if_needed(dataset):
    attention_kind = attention_kind_for_dataset(dataset)
    if not attention_kind:
        return False

    now = timezone.now()
    return bool(
        DatasetAttentionNotification.objects.filter(
            dataset=dataset,
            status=DatasetAttentionNotification.Status.PENDING,
        ).update(
            status=DatasetAttentionNotification.Status.READY,
            attention_kind=attention_kind,
            ready_at=now,
            next_attempt_at=now,
            updated_at=now,
        )
    )


def arm_notification(dataset, email):
    attention_kind = attention_kind_for_dataset(dataset)
    now = timezone.now()
    status = (
        DatasetAttentionNotification.Status.READY
        if attention_kind
        else DatasetAttentionNotification.Status.PENDING
    )
    defaults = {
        'email': email,
        'status': status,
        'attention_kind': attention_kind or '',
        'ready_at': now if attention_kind else None,
        'sent_at': None,
        'next_attempt_at': now if attention_kind else None,
        'attempt_count': 0,
        'last_error': '',
    }
    notification, _ = DatasetAttentionNotification.objects.update_or_create(
        dataset=dataset,
        defaults=defaults,
    )
    return notification


def cancel_notification(dataset):
    now = timezone.now()
    DatasetAttentionNotification.objects.filter(
        dataset=dataset,
        status__in=[
            DatasetAttentionNotification.Status.PENDING,
            DatasetAttentionNotification.Status.READY,
        ],
    ).update(
        status=DatasetAttentionNotification.Status.CANCELLED,
        next_attempt_at=None,
        updated_at=now,
    )


def notification_payload(dataset):
    try:
        notification = dataset.attention_notification
    except DatasetAttentionNotification.DoesNotExist:
        notification = None

    suggested_email = ''
    owner_email = (getattr(dataset.user, 'email', '') or '').strip()
    if owner_email and not owner_email.lower().endswith('@orcid.org'):
        suggested_email = owner_email

    return {
        'status': notification.status if notification else 'idle',
        'email': notification.email if notification else '',
        'suggested_email': suggested_email,
        'attention_kind': notification.attention_kind if notification else '',
        'sent_at': notification.sent_at if notification else None,
    }


def _email_content(notification):
    dataset = notification.dataset
    dataset_name = dataset.title.strip() if dataset.title else f'Dataset {dataset.id}'
    dataset_url = f"{settings.FRONTEND_URL.rstrip('/')}/dataset/{dataset.id}"
    if notification.attention_kind == DatasetAttentionNotification.AttentionKind.NEEDS_INPUT:
        subject = 'ChatIPT needs your attention'
        action = 'ChatIPT is waiting for your input.'
    elif notification.attention_kind == DatasetAttentionNotification.AttentionKind.PUBLISHED:
        subject = 'Your ChatIPT dataset has been published'
        action = 'Your dataset has been published.'
    else:
        subject = 'Your ChatIPT package is ready'
        action = 'Your package is ready to review and download.'

    body = (
        f'{action}\n\n'
        f'{dataset_name}\n'
        f'{dataset_url}\n\n'
        'You requested this one-time notification in ChatIPT.'
    )
    return subject, body


def _send_notification(notification):
    subject, body = _email_content(notification)
    send_mail(
        subject,
        body,
        settings.DEFAULT_FROM_EMAIL,
        [notification.email],
        fail_silently=False,
    )


def dispatch_due_notifications(limit=20):
    """Claim and send due notifications; safe to run from multiple workers."""
    if not settings.ATTENTION_EMAIL_ENABLED:
        return {'sent': 0, 'failed': 0, 'disabled': True}

    now = timezone.now()
    stale_before = now - timedelta(minutes=10)
    DatasetAttentionNotification.objects.filter(
        status=DatasetAttentionNotification.Status.SENDING,
        updated_at__lt=stale_before,
    ).update(
        status=DatasetAttentionNotification.Status.READY,
        next_attempt_at=now,
        updated_at=now,
    )

    candidate_ids = list(
        DatasetAttentionNotification.objects.filter(
            status=DatasetAttentionNotification.Status.READY,
        ).filter(
            Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now),
        ).order_by('ready_at', 'id').values_list('id', flat=True)[:limit]
    )
    result = {'sent': 0, 'failed': 0, 'disabled': False}

    for notification_id in candidate_ids:
        claimed = DatasetAttentionNotification.objects.filter(
            id=notification_id,
            status=DatasetAttentionNotification.Status.READY,
        ).update(
            status=DatasetAttentionNotification.Status.SENDING,
            attempt_count=models.F('attempt_count') + 1,
            updated_at=timezone.now(),
        )
        if not claimed:
            continue

        notification = DatasetAttentionNotification.objects.select_related('dataset').get(
            id=notification_id,
        )
        try:
            _send_notification(notification)
        except Exception as exc:
            delay_seconds = min(60 * (2 ** max(notification.attempt_count - 1, 0)), 3600)
            retry_at = timezone.now() + timedelta(seconds=delay_seconds)
            DatasetAttentionNotification.objects.filter(id=notification_id).update(
                status=DatasetAttentionNotification.Status.READY,
                next_attempt_at=retry_at,
                last_error=str(exc)[:2000],
                updated_at=timezone.now(),
            )
            logger.exception('Failed to send attention notification %s', notification_id)
            result['failed'] += 1
        else:
            sent_at = timezone.now()
            DatasetAttentionNotification.objects.filter(id=notification_id).update(
                status=DatasetAttentionNotification.Status.SENT,
                sent_at=sent_at,
                next_attempt_at=None,
                last_error='',
                updated_at=sent_at,
            )
            result['sent'] += 1

    return result
