import logging
import hashlib
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.db import models
from django.db.models import Q
from django.utils import timezone

from api.models import DatasetAttentionNotification, Message


logger = logging.getLogger(__name__)


def _ready_key(dataset):
    signature = f'{dataset.dwc_dp_url}:{dataset.dwca_url}'
    return 'ready:' + hashlib.sha256(signature.encode()).hexdigest()


def attention_event_for_dataset(dataset, ready_sent_key=''):
    """Return the dataset's current attention event as (kind, key).

    A given package is announced as ready once; after that, pauses on the same
    package are reported as ordinary requests for input.
    """
    if dataset.published_at:
        return DatasetAttentionNotification.AttentionKind.PUBLISHED, f'published:{dataset.published_at.isoformat()}'

    ready_key = _ready_key(dataset) if dataset.package_ready else ''
    unannounced_ready_key = ready_key if ready_key != ready_sent_key else ''
    active_agent = dataset.agent_set.filter(completed_at__isnull=True).order_by('created_at').first()
    if not active_agent:
        if unannounced_ready_key:
            return DatasetAttentionNotification.AttentionKind.READY, unannounced_ready_key
        return None, ''
    if active_agent.busy_thinking:
        return None, ''

    last_message = active_agent.message_set.order_by('-created_at').first()
    if not last_message or last_message.role != Message.Role.ASSISTANT:
        return None, ''
    if (last_message.openai_obj or {}).get('tool_calls'):
        return None, ''
    if (last_message.openai_obj or {}).get('workflow_error'):
        return DatasetAttentionNotification.AttentionKind.FAILED, f'message:{last_message.id}'
    if unannounced_ready_key:
        return DatasetAttentionNotification.AttentionKind.READY, unannounced_ready_key
    return DatasetAttentionNotification.AttentionKind.NEEDS_INPUT, f'message:{last_message.id}'


def _ready_sent_key(dataset):
    return (
        DatasetAttentionNotification.objects.filter(dataset=dataset)
        .values_list('ready_sent_key', flat=True)
        .first()
    ) or ''


def mark_notification_ready_if_needed(dataset):
    attention_kind, event_key = attention_event_for_dataset(dataset, _ready_sent_key(dataset))
    if not attention_kind:
        return False

    now = timezone.now()
    return bool(
        DatasetAttentionNotification.objects.filter(
            dataset=dataset,
            status=DatasetAttentionNotification.Status.PENDING,
        ).exclude(
            last_sent_event_key=event_key,
        ).update(
            status=DatasetAttentionNotification.Status.READY,
            attention_kind=attention_kind,
            ready_event_key=event_key,
            ready_at=now,
            next_attempt_at=now,
            updated_at=now,
        )
    )


def arm_notification(dataset, email):
    """Arm a one-shot email for the next attention event after this moment."""
    # The user is looking at the current state, so neither it nor a package
    # that is already ready should trigger the email.
    ready_sent_key = _ready_sent_key(dataset)
    current_kind, current_event_key = attention_event_for_dataset(dataset, ready_sent_key)
    if current_kind == DatasetAttentionNotification.AttentionKind.READY:
        ready_sent_key = current_event_key
        _, current_event_key = attention_event_for_dataset(dataset, ready_sent_key)
    defaults = {
        'email': email,
        'status': DatasetAttentionNotification.Status.PENDING,
        'attention_kind': '',
        'ready_event_key': '',
        'last_sent_event_key': current_event_key,
        'ready_sent_key': ready_sent_key,
        'ready_at': None,
        'sent_at': None,
        'next_attempt_at': None,
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
            DatasetAttentionNotification.Status.SENDING,
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
        'enabled': bool(notification and notification.status in {
            DatasetAttentionNotification.Status.PENDING,
            DatasetAttentionNotification.Status.READY,
            DatasetAttentionNotification.Status.SENDING,
        }),
        'delivery_available': settings.ATTENTION_EMAIL_ENABLED,
    }


def _email_content(notification):
    dataset = notification.dataset
    dataset_name = dataset.title.strip() if dataset.title else f'Dataset {dataset.id}'
    subject_dataset_name = dataset_name[:100]
    dataset_url = f"{settings.FRONTEND_URL.rstrip('/')}/dataset/{dataset.id}"
    if notification.attention_kind == DatasetAttentionNotification.AttentionKind.NEEDS_INPUT:
        subject = f'ChatIPT needs your attention: {subject_dataset_name}'
        action = 'ChatIPT is waiting for your input.'
    elif notification.attention_kind == DatasetAttentionNotification.AttentionKind.PUBLISHED:
        subject = f'Your ChatIPT dataset has been published: {subject_dataset_name}'
        action = 'Your dataset has been published.'
    elif notification.attention_kind == DatasetAttentionNotification.AttentionKind.FAILED:
        subject = f'ChatIPT processing stopped: {subject_dataset_name}'
        action = 'ChatIPT encountered a processing error and needs your attention.'
    else:
        subject = f'Your ChatIPT package is ready: {subject_dataset_name}'
        action = 'Your package is ready to review and download.'

    body = (
        f'Hello,\n\n'
        f'{action}\n\n'
        f'Dataset: {dataset_name}\n'
        f'Open ChatIPT: {dataset_url}\n\n'
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
        current_kind, current_event_key = attention_event_for_dataset(
            notification.dataset, notification.ready_sent_key,
        )
        if not current_kind or current_event_key == notification.last_sent_event_key:
            DatasetAttentionNotification.objects.filter(
                id=notification_id, status=DatasetAttentionNotification.Status.SENDING,
            ).update(status=DatasetAttentionNotification.Status.PENDING, next_attempt_at=None)
            continue
        if current_event_key != notification.ready_event_key:
            notification.attention_kind = current_kind
            notification.ready_event_key = current_event_key
            DatasetAttentionNotification.objects.filter(
                id=notification_id, status=DatasetAttentionNotification.Status.SENDING,
            ).update(attention_kind=current_kind, ready_event_key=current_event_key)
        try:
            _send_notification(notification)
        except Exception as exc:
            delay_seconds = min(60 * (2 ** max(notification.attempt_count - 1, 0)), 3600)
            retry_at = timezone.now() + timedelta(seconds=delay_seconds)
            DatasetAttentionNotification.objects.filter(
                id=notification_id, status=DatasetAttentionNotification.Status.SENDING,
            ).update(
                status=DatasetAttentionNotification.Status.READY,
                next_attempt_at=retry_at,
                last_error=str(exc)[:2000],
                updated_at=timezone.now(),
            )
            logger.exception('Failed to send attention notification %s', notification_id)
            result['failed'] += 1
        else:
            sent_at = timezone.now()
            sent_fields = {}
            if notification.attention_kind == DatasetAttentionNotification.AttentionKind.READY:
                sent_fields['ready_sent_key'] = notification.ready_event_key
            DatasetAttentionNotification.objects.filter(
                id=notification_id, status=DatasetAttentionNotification.Status.SENDING,
            ).update(
                status=DatasetAttentionNotification.Status.SENT,
                sent_at=sent_at,
                last_sent_event_key=notification.ready_event_key,
                next_attempt_at=None,
                last_error='',
                updated_at=sent_at,
                **sent_fields,
            )
            result['sent'] += 1

    return result
