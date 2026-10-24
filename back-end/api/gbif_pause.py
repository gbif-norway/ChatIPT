"""Manual switch that pauses all ChatIPT processing while part of GBIF's API is down.

While an outage is open, the turn workers claim no agent turns or conversion jobs, an
agent starts no new turn, and requests that would queue new work are refused with 503.
Queued work keeps its place and continues once the outage ends. Waits on the GBIF
validator leave paused time out of their limits (active_seconds_since).
"""
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import APIException

from api.helpers import discord_bot
from api.models import GbifOutage

DEFAULT_NOTICE = (
    "Part of GBIF's API isn't working right now, so ChatIPT has paused all datasets. "
    "Your saved work is safe and will continue automatically when GBIF is back. "
    "Please try again later."
)


class GbifPaused(APIException):
    status_code = 503
    default_code = 'gbif_paused'


def current():
    """The open outage, or None."""
    return GbifOutage.objects.filter(ended_at__isnull=True).first()


def is_paused():
    return GbifOutage.objects.filter(ended_at__isnull=True).exists()


def notice(outage):
    return outage.message.strip() or DEFAULT_NOTICE


def status():
    outage = current()
    if not outage:
        return {'paused': False}
    return {'paused': True, 'message': notice(outage), 'since': outage.started_at.isoformat()}


def refuse_new_work():
    """Raise a 503 for a request that would queue processing while paused."""
    outage = current()
    if outage:
        raise GbifPaused(notice(outage))


def start(message='', by=''):
    """Open an outage. Returns (outage, created); an open outage is left as it is."""
    try:
        with transaction.atomic():
            outage = current()
            if outage:
                return outage, False
            outage = GbifOutage.objects.create(message=message, started_by=by)
            transaction.on_commit(lambda: discord_bot.send_discord_message(
                f"⏸️ ChatIPT paused all processing for a GBIF outage{f' ({by})' if by else ''}. "
                f"Users see: {notice(outage)}"
            ))
    except IntegrityError:  # another request opened one first
        return current(), False
    return outage, True


def end(by=''):
    """Close the open outage and return it; None when nothing was paused."""
    with transaction.atomic():
        outage = GbifOutage.objects.select_for_update().filter(ended_at__isnull=True).first()
        if not outage:
            return None
        outage.ended_at = timezone.now()
        outage.ended_by = by
        outage.save(update_fields=['ended_at', 'ended_by'])
        minutes = int((outage.ended_at - outage.started_at).total_seconds() // 60)
        transaction.on_commit(lambda: discord_bot.send_discord_message(
            f"▶️ ChatIPT resumed processing after {minutes} minutes paused for a GBIF outage"
            f"{f' ({by})' if by else ''}."
        ))
    return outage


def _aware(value):
    return timezone.make_aware(value) if timezone.is_naive(value) else value


def paused_seconds(since, until=None):
    """Seconds between `since` and `until` (default now) that fell inside outages."""
    since, until = _aware(since), _aware(until or timezone.now())
    outages = GbifOutage.objects.filter(started_at__lt=until).filter(
        Q(ended_at__isnull=True) | Q(ended_at__gt=since)
    )
    return sum(
        max((min(ended or until, until) - max(started, since)).total_seconds(), 0)
        for started, ended in outages.values_list('started_at', 'ended_at')
    )


def active_seconds_since(since, until=None):
    """Seconds elapsed since `since`, leaving out time ChatIPT was paused for GBIF."""
    since, until = _aware(since), _aware(until or timezone.now())
    return (until - since).total_seconds() - paused_seconds(since, until)
