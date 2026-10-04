"""Issue policy, grouped row decisions and requirement checks. Pure functions of plan + decisions.

See docs/dwca-conversion/tiered-review.md. Nothing here reads the archive: archive
evidence is precomputed into plan['requirements'] by api.dwca_preflight.
"""
from __future__ import annotations

import json
from collections import defaultdict

ALL = '*'
# Options that assert a new fact. These are never applied by an AI reviewer alone.
ISSUE_POLICY = {
    'layout': ALL,
    'taxonomy-package': (),
    'event-grain': (),  # per_row becomes an assertion when supplied eventIDs repeat (set by the plan).
    'event-category': ALL,
    'occurrence-status': ALL,
    'extension-role': ('media-occurrence', 'media-event'),
    'taxon-occurrences': ('convert',),
    'material-identity': ALL,
    'column-mapping': (),
    'name-semantics': (),
    'external-identifier': ALL,
    'row-handling': (),
    'trait-link': (),
    'survey-classification': ('confirm',),
    'survey-completeness': ALL,
    # Treating every occurrence row as its own event asserts those events exist; copying agreeing values does not.
    'occurrence-events': ('per-row',),
}
_PREFIX_KINDS = (
    ('loose-links', 'layout'), ('taxonomy-package', 'taxonomy-package'), ('event-grain', 'event-grain'),
    ('event-category', 'event-category'), ('status:', 'occurrence-status'), ('table:', 'extension-role'),
    ('material:', 'material-identity'), ('column:', 'column-mapping'), ('row-group:', 'row-handling'),
    ('row:', 'row-handling'), ('trait-link:', 'trait-link'), ('hum-category:', 'survey-classification'),
    ('hum-scope-group:', 'survey-completeness'), ('hum-scope:', 'survey-completeness'),
    ('occurrence-events:', 'occurrence-events'),
)
GROUP_PREFIXES = {'row': 'row-group', 'hum-scope': 'hum-scope-group'}
SKIPPED_TARGETS = {'preserve', 'join', 'derive'}


def _local_id(issue_id):
    while issue_id.startswith('taxon-occurrence:'):
        issue_id = issue_id.split(':', 2)[2]
    return issue_id


def infer_kind(issue_id):
    local = _local_id(issue_id)
    return next(kind for prefix, kind in _PREFIX_KINDS if local == prefix or local.startswith(prefix))


def apply_policy(entries):
    """Set kind, per-option assertion flags and the derived authority. Idempotent."""
    for entry in entries:
        kind = entry.setdefault('kind', infer_kind(entry['id']))
        policy = ISSUE_POLICY[kind]
        explicit = entry.pop('assertion_values', None)
        asserted = set(explicit) if explicit is not None else None
        entry['options'] = [option if 'assertion' in option else {**option, 'assertion': option['value'] != 'preserve' and (
            option['value'] in asserted if asserted is not None else policy == ALL or option['value'] in policy)}
            for option in entry['options']]
        claims = [option['assertion'] for option in entry['options'] if option['value'] != 'preserve']
        entry['authority'] = 'user-assertion' if claims and all(claims) else 'ai-reviewable'
    return entries


def group_rows(row_issues, table_names):
    """Group row issues with identical kind, table, reason and options (and scope values).

    Returns (issues, members): single-row groups keep their member issue; members
    lists every grouped member as {id, group, table, row, title, reason}. Group numbering follows
    each table's first source row and never depends on decisions.
    """
    groups = {}
    for issue in row_issues:
        family = 'hum-scope' if _local_id(issue['id']).startswith('hum-scope:') else 'row'
        key = (family, issue['table'], issue.get('kind', ''), issue['reason'],
               json.dumps([[option['value'], option['label']] for option in issue['options']], ensure_ascii=False),
               json.dumps(issue.get('scope_values'), sort_keys=True, ensure_ascii=False))
        groups.setdefault(key, []).append(issue)
    issues, members, counters = [], [], defaultdict(int)
    for key, items in sorted(groups.items(), key=lambda item: (item[1][0]['table'], item[1][0]['row'], item[0][0])):
        family, table = key[0], key[1]
        items = sorted(items, key=lambda issue: issue['row'])
        if len(items) == 1:
            issues.append(items[0])
            continue
        group_id = f"{GROUP_PREFIXES[family]}:{table}:{counters[(family, table)]}"
        counters[(family, table)] += 1
        first = items[0]
        suffix = first['title'].split(': ', 1)[-1]
        group = {key: value for key, value in first.items() if key not in {'id', 'title', 'row', 'scope_values'}}
        group.update(id=group_id, title=f"{table_names[table]}, {len(items)} rows: {suffix}",
                     members=[issue['id'] for issue in items], rows=[issue['row'] for issue in items],
                     count=len(items), sample_rows=[issue['row'] for issue in items[:5]])
        if first.get('scope_values') is not None:
            group['scope_values'] = first['scope_values']
        issues.append(group)
        members.extend({'id': issue['id'], 'group': group_id, 'table': table, 'row': issue['row'],
                        'title': issue['title'], 'reason': issue['reason']} for issue in items)
    return issues, members


def effective_decisions(plan, decisions):
    """Automatic defaults, then group choices expanded to members, then explicit choices."""
    automatic = {item['id']: item['default'] for item in plan.get('automatic_choices', [])}
    effective = dict(automatic)
    for entry in [*plan.get('issues', []), *plan.get('automatic_choices', [])]:
        value = decisions.get(entry['id'], automatic.get(entry['id']))
        if value is not None and entry.get('members'):
            effective.update(dict.fromkeys(entry['members'], value))
    effective.update(decisions)
    return effective


def _columns(plan):
    return {column['id']: column for column in plan.get('columns', [])}


def column_target(plan, effective, column):
    """The column's effective target, accounting for preserved tables and material."""
    t = column['table']
    tables = plan.get('tables', [])
    if t < len(tables) and not tables[t]['core'] and effective.get(f'table:{t}') == 'preserve':
        return 'preserve'
    target = effective.get(column['id'], column['default'])
    if target.startswith('material.') and effective.get(f'material:{t}', 'preserve') == 'preserve':
        return 'preserve'
    return target


def _holds(plan, effective, condition, columns):
    kind = condition['type']
    if kind == 'unsatisfiable':
        return False
    if kind == 'decision_in':
        return effective.get(condition['id']) in condition['values']
    if kind == 'any':
        return any(_holds(plan, effective, inner, columns) for inner in condition['conditions'])
    if kind == 'all':
        return all(_holds(plan, effective, inner, columns) for inner in condition['conditions'])
    if kind in {'target_in', 'target_not_in'}:
        target = column_target(plan, effective, columns[condition['column']])
        hit = target in condition.get('targets', ()) or any(target.startswith(prefix) for prefix in condition.get('prefixes', ()))
        return hit if kind == 'target_in' else not hit
    raise ValueError(f'Unknown requirement condition: {kind}')


def _referenced(condition):
    if condition['type'] in {'any', 'all'}:
        return [identifier for inner in condition['conditions'] for identifier in _referenced(inner)]
    return [condition[key] for key in ('id', 'column') if key in condition]


def failed_requirements(plan, effective, decision_id, value, columns=None):
    columns = columns if columns is not None else _columns(plan)
    return [requirement for requirement in plan.get('requirements', {}).get(decision_id, {}).get(value, [])
            if all(_holds(plan, effective, condition, columns) for condition in requirement.get('when', []))
            and not all(_holds(plan, effective, condition, columns) for condition in requirement['conditions'])]


def _entries(plan):
    entries = {item['id']: item for item in [*plan.get('issues', []), *plan.get('automatic_choices', [])]}
    groups = {item['id']: item for item in entries.values() if item.get('members')}
    for member in plan.get('row_issues', []):
        group = groups.get(member['group'])
        if group:
            entries[member['id']] = {**group, 'id': member['id'], 'row': member['row'], 'members': None}
    return entries


def _active(plan, effective, entry):
    """Choices under a preserved extension table or preserved row do not apply."""
    t = entry.get('table')
    if t is None or entry['id'] == f'table:{t}':
        return True
    tables = plan.get('tables', [])
    if t < len(tables) and not tables[t]['core'] and effective.get(f'table:{t}') == 'preserve':
        return False
    return not ('row' in entry and effective.get(f"row:{t}:{entry['row'] - 1}") == 'preserve'
                and not entry['id'].startswith('row:'))


def violations(plan, decisions):
    """Active effective choices (explicit, automatic, group-expanded, column defaults) whose requirements fail."""
    effective = effective_decisions(plan, decisions)
    columns = _columns(plan)
    entries = _entries(plan)
    found = []
    for decision_id, by_value in plan.get('requirements', {}).items():
        if decision_id in columns:
            column = columns[decision_id]
            value = column_target(plan, effective, column)
        else:
            entry = entries.get(decision_id)
            value = effective.get(decision_id)
            if value is None or (entry is not None and not _active(plan, effective, entry)):
                continue
        failed = failed_requirements(plan, effective, decision_id, value, columns) if value in by_value else []
        if failed:
            found.append({'id': decision_id, 'value': value, 'reasons': [requirement['reason'] for requirement in failed],
                          'decision_ids': list(dict.fromkeys([decision_id, *(identifier for requirement in failed
                                                                            for condition in [*requirement.get('when', []), *requirement['conditions']]
                                                                            for identifier in _referenced(condition))])),
                          'evidence': [requirement.get('evidence', {}) for requirement in failed]})
    return found


def option_status(plan, decisions, prefix=''):
    """{decision_id: {value: {available, reasons}}} for options with requirements or unavailable options."""
    effective = effective_decisions(plan, decisions)
    columns = _columns(plan)
    status = {}
    for decision_id, by_value in plan.get('requirements', {}).items():
        for value in by_value:
            failed = failed_requirements(plan, effective, decision_id, value, columns)
            status.setdefault(prefix + decision_id, {})[value] = {
                'available': not failed, 'reasons': [requirement['reason'] for requirement in failed]}
    for entry in [*plan.get('issues', []), *plan.get('automatic_choices', []), *plan.get('columns', [])]:
        for option in entry.get('unavailable_options', []):
            status.setdefault(prefix + entry['id'], {})[option['value']] = {
                'available': False, 'reasons': [option['reason']], 'unsatisfiable': True}
    for index, inner in plan.get('taxonomy', {}).get('occurrence_plans', {}).items():
        if effective.get(f'table:{index}') == 'preserve':
            continue
        inner_prefix = f'taxon-occurrence:{index}:'
        inner_decisions = {key[len(inner_prefix):]: value for key, value in decisions.items() if key.startswith(inner_prefix)}
        status.update(option_status(inner, inner_decisions, prefix + inner_prefix))
    return status


def remove_unavailable(plan_entries, unavailable):
    """Move unsatisfiable options out of option lists, recording reasons and evidence."""
    for entry in plan_entries:
        blocked = unavailable.get(entry['id'])
        if not blocked:
            continue
        kept = [option for option in entry['options'] if option['value'] not in blocked]
        removed = [{**option, **blocked[option['value']]} for option in entry['options'] if option['value'] in blocked]
        if not removed:
            continue
        entry['options'] = kept
        entry['unavailable_options'] = [*entry.get('unavailable_options', []), *removed]
        if entry.get('default') in blocked:
            entry['default'] = kept[0]['value'] if kept else 'preserve'
