"""Django-facing state and provenance for deterministic value tidy-up."""
import json
import logging

from django.conf import settings

from api.dwca_tidy import TIDY_VERSION, summarize, tidy_archive as apply_tidy

logger = logging.getLogger(__name__)


def enabled():
    return getattr(settings, 'CONVERSION_TIDY_ENABLED', True)


def _stored_overrides(conversion, archive):
    state = conversion.tidy if isinstance(conversion.tidy, dict) else {}
    if state.get('source_sha256') != archive.fingerprint or state.get('version') != TIDY_VERSION:
        return {}
    return state.get('overrides', {})


def overrides(conversion, archive):
    return _stored_overrides(conversion, archive)


def tidied(conversion, archive, overrides=None):
    if not enabled():
        return archive
    return apply_tidy(archive, overrides=_stored_overrides(conversion, archive) if overrides is None else overrides)[0]


def record(conversion, view, overrides):
    if view.tidy is None:
        conversion.tidy = {}
        return
    conversion.tidy = {key: view.tidy[key] for key in ('version', 'source_sha256', 'sha256')}
    conversion.tidy.update(overrides=overrides or {}, summary=summarize(view.tidy))


def known_ids(conversion):
    summary = (conversion.tidy or {}).get('summary', {})
    return {identifier for group in summary.get('groups', [])
            for identifier in [group.get('id'), *(item.get('id') for item in group.get('values', []))] if identifier}


def request_changes(conversion, changes):
    if not isinstance(changes, dict) or len(changes) > 500:
        raise ValueError('Tidy changes must be a mapping with at most 500 entries.')
    unknown = set(changes) - known_ids(conversion)
    if unknown:
        raise ValueError(f'Unknown tidy change id: {sorted(unknown)[0]}.')
    if any(value is not None and value not in ('undo', 'apply') for value in changes.values()):
        raise ValueError('Tidy changes must be undo, apply, or null.')
    result = dict((conversion.tidy or {}).get('overrides', {}))
    for identifier, action in changes.items():
        if action is None:
            result.pop(identifier, None)
        else:
            result[identifier] = 'off' if action == 'undo' else 'on'
    conversion.tidy['pending_overrides'] = result
    return result


def state_section(conversion, job=None):
    from api.models import DwcConversionJob
    state = conversion.tidy if isinstance(conversion.tidy, dict) else {}
    summary = state.get('summary', {})
    job = job if job is not None else DwcConversionJob.objects.filter(conversion=conversion).first()
    groups = summary.get('groups', [])
    return {'enabled': enabled(), 'pending': bool(job and job.action == 'replan'), 'groups': groups,
            'overrides': state.get('overrides', {}),
            'counts': {'tidied_groups': sum(bool(g.get('applied')) and g.get('tier') == 'auto' for g in groups),
                       'tidied_rows': sum(sum(v.get('changed_rows', 0) for v in g.get('values', []))
                                          for g in groups if g.get('applied')),
                       'suggestions': sum(1 for g in groups for v in g.get('values', [])
                                          if g.get('tier') == 'suggest' and not v.get('applied'))}}


def report_section(conversion, view):
    if view.tidy is None:
        return {}
    return {'version': view.tidy['version'], 'sha256': view.tidy['sha256'],
            'overrides': (conversion.tidy or {}).get('overrides', {}),
            'policy': 'Obvious, reversible value clean-ups were applied before conversion; original files are unchanged.',
            'groups': view.tidy['groups'], 'added_columns': view.tidy['added_columns']}


def commit_carry(conversion):
    """Inside the fenced store of a replan: copy carried provenance and move the conversation to the new plan."""
    from api.models import DwcConversionDecisionEvent
    copies, old_id, new_id = getattr(conversion, '_tidy_carry', ([], '', ''))
    if copies:
        DwcConversionDecisionEvent.objects.bulk_create(copies)
    if old_id and new_id:
        conversion.messages.filter(plan_id=old_id).update(plan_id=new_id)


def _items(plan):
    return {item['id']: item for item in [*plan.get('columns', []), *plan.get('issues', []),
            *plan.get('automatic_choices', []), *plan.get('row_issues', [])]}


def carry_plan_state(conversion, old_plan, new_plan, view):
    from api.conversion_review import latest_events
    from api.dwca_conversion import validate_decisions
    from api.dwca_import import ConversionError
    old_items, new_items = _items(old_plan), _items(new_plan)
    old_events = latest_events(conversion)
    kept = {}
    dropped = []
    for identifier, value in conversion.decisions.items():
        item, fresh = old_items.get(identifier), new_items.get(identifier)
        if not item or not fresh:
            dropped.append(identifier); continue
        if identifier.startswith('column:') and item.get('term') != fresh.get('term'):
            dropped.append(identifier); continue
        group = fresh.get('group')
        options_item = new_items.get(group, fresh) if group else fresh
        if value not in {option['value'] for option in options_item.get('options', [])}:
            dropped.append(identifier); continue
        event = old_events.get(identifier)
        if event and event.source == 'ai-reviewer' and json.dumps(item, sort_keys=True) != json.dumps(fresh, sort_keys=True):
            dropped.append(identifier); continue
        kept[identifier] = value
    for _ in range(20):
        try:
            validate_decisions(new_plan, kept, require_complete=False)
            break
        except ConversionError as exc:
            invalid = exc.decision_ids
            if not invalid:
                dropped.extend(kept)
                kept = {}
                break
            for identifier in invalid:
                if identifier in kept:
                    kept.pop(identifier); dropped.append(identifier)
    conversion.decisions = kept
    from api.models import DwcConversionDecisionEvent
    copies = []
    for identifier, value in kept.items():
        event = old_events.get(identifier)
        if event:
            copies.append(DwcConversionDecisionEvent(conversion=conversion, plan_id=new_plan['id'], decision_id=identifier,
                value=event.value, previous_value=event.previous_value, source=event.source, model=event.model,
                reasoning_effort=event.reasoning_effort, response_id=event.response_id, confidence=event.confidence,
                evidence=event.evidence, rationale=event.rationale, message=event.message,
                transcript={'carried_from_plan': old_plan['id']}))
    conversion._tidy_carry = (copies, old_plan.get('id', ''), new_plan['id'])
    old_review = conversion.review if isinstance(conversion.review, dict) else {}
    recommendations = {key: {**record, **({'plan_id': new_plan['id']} if 'plan_id' in record else {})}
                        for key, record in old_review.get('recommendations', {}).items()
                        if key in old_items and key in new_items and json.dumps(old_items[key], sort_keys=True) == json.dumps(new_items[key], sort_keys=True)}
    conversion.review = {'plan_id': new_plan['id'], 'runs': old_review.get('runs', 0), 'status': 'idle',
                         'error': '', 'recommendations': recommendations}
    from api.conversion_names import collect_state, carry_decisions
    try:
        fresh_names = collect_state(view, new_plan)
        old_names = conversion.name_review if isinstance(conversion.name_review, dict) else {}
        keys = ('label', 'rows', 'tables', 'hints', 'source_rank', 'qualifier', 'source_authorships')
        if old_names.get('plan_id') == old_plan.get('id') and [tuple(label.get(k) for k in keys) for label in old_names.get('labels', [])] == [tuple(label.get(k) for k in keys) for label in fresh_names.get('labels', [])]:
            conversion.name_review = {**old_names, 'plan_id': new_plan['id']}
        else:
            conversion.name_review = carry_decisions(conversion, fresh_names, new_plan)
    except Exception:
        logger.exception('Could not carry scientific names to the replanned conversion %s', conversion.pk)
        # The tidy-up never changes names, so the previous name review still describes them.
        old_names = conversion.name_review if isinstance(conversion.name_review, dict) else {}
        conversion.name_review = {**old_names, 'plan_id': new_plan['id']} if old_names.get('plan_id') == old_plan.get('id') else {}
    return {'carried': len(kept), 'dropped': sorted(set(dropped))}
