"""Adapt the chat workflow's stored rows to the shared scientific-name review."""
from django.utils import timezone

from api import name_review, taxon_matching


def record(row):
    supplied = row.supplied or {}
    result = {
        **supplied,
        'key': str(getattr(row, 'id', row.verbatim_label)), 'label': row.verbatim_label, 'rows': row.record_count,
        'tables': {getattr(row, 'source_table', 'occurrence'): row.record_count},
        'qualifier': row.identification_qualifier or None,
        'source_authorships': supplied.get('source_authorships', supplied.get('authorships', [])),
        'authorships_truncated': supplied.get('authorships_truncated', supplied.get('truncated', False)),
        'source_rank': supplied.get('source_rank', supplied.get('rank')),
        'hints': {**{rank: row.query[rank] for rank in taxon_matching.HINT_RANKS if row.query.get(rank)},
                  **supplied.get('hints', {})},
        'preprocessed': taxon_matching.is_preprocessed(row),
        'context_key': getattr(row, 'context_key', ''), 'query': row.query,
        'preprocessing_note': getattr(row, 'preprocessing_note', ''), 'review_aids': getattr(row, 'review_aids', {}),
    }
    if row.matched_at:
        result.update(match=row.match, parsed=row.parsed or {}, col_release=getattr(row, 'col_release', {}))
    return result


def snapshot(row):
    from api.models import TaxonNameMatch

    if row.decision == TaxonNameMatch.Decision.PENDING:
        return None
    usage = row.decided_usage or {}
    kind = usage.get('decision') or taxon_matching.decided_kind(row)
    decision = {**usage, 'decision': kind, 'by': usage.get('by') or 'user',
                'usageId': usage.get('usageId', usage.get('id')),
                'at': usage.get('at') or (row.decided_at.isoformat() if row.decided_at else None)}
    release = getattr(row, 'col_release', {}) or {}
    match = row.match or {}
    if kind == 'col' and not usage.get('decision') and usage.get('source') == 'gbif_col':
        # The original review stored the chosen GBIF usage directly. Verify that
        # it belongs to this saved match before attaching its release.
        choices = [match.get('usage') or {}, *(match.get('alternatives') or [])]
        if decision['usageId'] and any(str(choice.get('id')) == str(decision['usageId']) for choice in choices):
            decision['taxonomicStatus'] = usage.get('status')
            decision.setdefault('checklist', {key: release.get(key) for key in ('checklistKey', 'alias')})
    return name_review.with_checklist_release(decision, release, match)


def effective_snapshot(row):
    decision = snapshot(row)
    if decision and name_review._unconfirmed_replacement(record(row), decision):
        return {'decision': 'keep'}
    return decision


def state(rows):
    records = [record(row) for row in rows]
    choices = {}
    for row in rows:
        choices.update((row.review_meta or {}).get('group_choices') or {})
    return {
        'status': 'complete' if all(row.matched_at for row in rows) else 'pending',
        'labels': records,
        'decisions': {str(row.id): snapshot(row) for row in rows if snapshot(row)},
        'auto_declined': [str(row.id) for row in rows if (row.review_meta or {}).get('auto_declined')],
        'group_choices': choices,
        'col_release': next((row.col_release for row in rows if row.col_release), {}),
    }


def save_state(rows, review, user=None):
    kinds = {'col': 'accepted', 'alternative': 'accepted', 'stem': 'accepted',
             'parsed': 'keep_original', 'keep': 'keep_original', 'corrected': 'corrected', 'empty': 'empty'}
    for row in rows:
        key = str(row.id)
        decided = review['decisions'].get(key)
        meta = {'auto_declined': key in review.get('auto_declined', []),
                'group_choices': review.get('group_choices') or {}}
        if decided == snapshot(row):
            if meta != row.review_meta:
                row.review_meta = meta
                row.save(update_fields=['review_meta', 'updated_at'])
            continue
        if decided is None:
            taxon_matching._reset_decision(row)
        else:
            row.decision = kinds[decided['decision']]
            row.decided_usage = decided
            row.decided_query = row.query
            row.decided_at = timezone.datetime.fromisoformat(decided['at']) if decided.get('at') else timezone.now()
            row.decided_by = user if decided.get('by') == 'user' or decided.get('by', '').startswith('bulk:') else None
        row.review_meta = meta
        row.save(update_fields=['decision', 'decided_usage', 'decided_query', 'decided_at', 'decided_by', 'review_meta', 'updated_at'])


def initialize(rows):
    review = state(rows)
    name_review.auto_accept(review)
    save_state(rows, review)


def section(rows, **page):
    return name_review.state_section(state(rows), **page)


def change(rows, payload, user=None):
    review = state(rows)
    if 'name_decisions' in payload:
        name_review.set_decisions(review, payload['name_decisions'])
    elif 'group_choice' in payload:
        choice = payload['group_choice']
        if not isinstance(choice, dict):
            raise ValueError('Choose a name group and group decision.')
        name_review.group_choice(review, choice.get('group'), choice.get('choice'))
    else:
        raise ValueError('Choose a name or group decision.')
    save_state(rows, review, user)
