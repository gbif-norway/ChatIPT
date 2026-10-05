"""Resolve supplied dwc:parentEventID values against persistent dwc:eventID values.

Pure and deterministic. Iterative traversal only, so deep hierarchies never reach
Python's recursion limit. Archive join keys (meta.xml id/coreid) are never used as
parent targets; they are accepted only to explain a non-matching parent value.
"""
from __future__ import annotations

from collections import defaultdict

MAX_REPORTED_PROBLEMS = 50
PROBLEM_REASONS = {
    'missing': 'No source Event row has this persistent eventID.',
    'ambiguous': 'Several source Event rows share this persistent eventID; none is selected.',
    'self': 'The event names itself as its parent.',
    'cycle': 'Parent links form a cycle.',
    'inconsistent': 'Rows combined into one event supply different parentEventID values.',
    'no-event-id': 'A row with a parentEventID has no eventID, so its event identity is not established.',
}

# These are common empty-cell tokens in exported archives. They are interpreted
# as absent only when no source record actually uses the token as its ID.
EMPTY_REFERENCE_TOKENS = frozenset({'na', 'n/a', 'null', 'none'})


def missing_reference(value, known_identifiers):
    return bool(value) and value not in known_identifiers and value.strip().casefold() in EMPTY_REFERENCE_TOKENS


def resolve_parents(nodes, archive_keys=()):
    """Resolve parent links between event nodes.

    ``nodes`` is a sequence of dicts with ``event_id`` (supplied persistent eventID,
    possibly empty), ``parents`` (the distinct supplied parentEventID values of the
    node's source rows, including '' for rows without one) and ``rows`` (source row
    numbers, for reporting). Node positions identify nodes; callers map them back to
    emitted event keys. Values are compared exactly, without trimming or case folding.

    Returns only individually resolvable links. Invalid, ambiguous, self and
    cyclic links remain in the originals and are reported separately.
    """
    archive_keys = set(archive_keys)
    by_event_id = defaultdict(list)
    for position, node in enumerate(nodes):
        if node['event_id']:
            by_event_id[node['event_id']].append(position)
    problems, candidate = [], {}
    for position, node in enumerate(nodes):
        supplied = sorted(value for value in set(node['parents']) if value)
        if not supplied:
            continue
        base = {'eventID': node['event_id'], 'rows': sorted(node['rows'])}
        if len(set(node['parents'])) > 1:
            problems.append({**base, 'problem': 'inconsistent', 'parentEventID': ' | '.join(sorted(set(node['parents'])))})
            continue
        parent = supplied[0]
        base['parentEventID'] = parent
        if not node['event_id']:
            problems.append({**base, 'problem': 'no-event-id'})
            continue
        if parent == node['event_id']:
            problems.append({**base, 'problem': 'self'})
            continue
        targets = by_event_id.get(parent, [])
        if not targets:
            problem = {**base, 'problem': 'missing'}
            if parent in archive_keys:
                problem['note'] = 'The value matches a meta.xml core id. Archive join keys are not persistent eventIDs and are never used as parents.'
            problems.append(problem)
        elif len(targets) > 1:
            problems.append({**base, 'problem': 'ambiguous',
                             'candidate_rows': sorted(row for target in targets for row in nodes[target]['rows'])})
        else:
            candidate[position] = targets[0]
    cycles, cyclic_children = _cycles(candidate, nodes)
    problems.extend(cycles)
    for child in cyclic_children:
        candidate.pop(child, None)
    depth = _max_depth(candidate)
    problems.sort(key=lambda item: (item['rows'][:1], item['problem'], item.get('parentEventID', '')))
    return {
        'links': dict(sorted(candidate.items())),
        'problems': problems,
        'counts': {
            'events': len(nodes),
            'with_parent': sum(any(node['parents']) for node in nodes),
            'resolvable': len(candidate),
            'roots': sum(not any(node['parents']) for node in nodes),
            'max_depth': depth,
            'problems': len(problems),
        },
    }


def _cycles(links, nodes):
    """Report each cycle once, iteratively, starting from its lowest position."""
    state, cycles, cyclic_children = {}, [], set()
    for start in sorted(links):
        if start in state:
            continue
        path, index, node = [], {}, start
        while node is not None and node not in state:
            index[node] = len(path); path.append(node); state[node] = 'active'
            node = links.get(node)
        if node is not None and state.get(node) == 'active':
            members = path[index[node]:]
            cyclic_children.update(members)
            first = members.index(min(members))
            members = members[first:] + members[:first]
            cycles.append({'problem': 'cycle', 'eventIDs': [nodes[member]['event_id'] for member in members],
                           'parentEventID': nodes[members[0]]['event_id'],
                           'rows': sorted(row for member in members for row in nodes[member]['rows'])})
        for member in path:
            state[member] = 'done'
    return cycles, cyclic_children


def _max_depth(links):
    depth = {}
    for start in links:
        chain, node = [], start
        while node in links and node not in depth:
            chain.append(node); node = links[node]
        base = depth.get(node, 0)
        for offset, member in enumerate(reversed(chain), start=1):
            depth[member] = base + offset
    return max(depth.values(), default=0)


def summary(result):
    """Plan/report summary with a bounded problem sample."""
    return {**result['counts'], 'problem_kinds': dict(sorted(_kinds(result['problems']).items())),
            'problem_sample': [{**problem, 'reason': PROBLEM_REASONS[problem['problem']]}
                               for problem in result['problems'][:MAX_REPORTED_PROBLEMS]]}


def _kinds(problems):
    counts = defaultdict(int)
    for problem in problems:
        counts[problem['problem']] += 1
    return counts


def describe_problems(result):
    kinds = _kinds(result['problems'])
    first = result['problems'][0]
    example = f" First: {first['problem']} at source row(s) {', '.join(map(str, first['rows'][:5]))}"
    example += f" (parentEventID {first['parentEventID']!r})." if first.get('parentEventID') else '.'
    return ('Parent events cannot be linked faithfully: '
            + ', '.join(f'{count} {kind}' for kind, count in sorted(kinds.items())) + '.' + example)
