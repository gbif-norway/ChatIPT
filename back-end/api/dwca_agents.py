"""Conservative Agent and *-agent-role rows from mapped DwC-DP records. No network calls.

The mapped records keep their verbatim name fields (recordedBy, identifiedBy, ...).
This module only adds role rows that link those records to Agent rows:

- A single explicit agent IRI in the paired *ByID field links to the Agent row
  with that agentID, reusing the converter's row when there is one.
- A name-only value remains in its mapped text field unless review confirms
  that every mention of that exact (whitespace-normalised) name denotes one
  agent. A confirmed name creates one Agent row, with a role row per mention.
- A ``|``-delimited name list whose paired *ByID field is a ``|``-delimited
  list of single agent IRIs of the same length is split pairwise, in order.
- Other lists, conjunctions, ``et al.``, placeholders and ID/name pairs that
  cannot be matched one-to-one are never split or guessed. They are skipped
  and reported, and the source value stays in the mapped field and originals.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping

from api.dwc_dp_specs import TABLE_SPECS

EXAMPLE_LIMIT = 20
SHARED_NAME_REMARK = ('Name-only source value. Review asserted that every name-only mention with '
                      'this exact name refers to this one agent.')
POLICY = ('Role rows link mapped records to agents without changing the mapped name fields. A single '
          'absolute IRI in the paired *ByID field links to the agent with that agentID. A name-only '
          'value stays in its mapped text field unless review confirms that every mention of its exact '
          '(whitespace-normalised) name denotes one agent. A |-delimited name list is split only '
          'when its *ByID field lists the same number of single IRIs, paired in order. Other lists, '
          'conjunctions, et al., placeholders and ID/name pairs that cannot be matched one-to-one are '
          'skipped and reported, never split. agentRole is the DwC-DP field name; agentRoleIRI and '
          'agentRoleSource are left empty.')
PLACEHOLDERS = frozenset({
    'na', 'n/a', 'n.a.', 'nan', 'none', 'null', 'nil', 'unknown', 'unk', 'unkn', 'anonymous', 'anon',
    'anon.', 'not recorded', 'not available', 'not known', 'missing', 'ukjent', 'ikke oppgitt',
    '?', '??', '-', '--', '.', 's.n.', 'leg.', 'det.',
})
# "and" in languages common in GBIF Norway and European archives. Compound
# surnames such as "Ortega y Gasset" are skipped too: skipping is reported,
# whereas splitting would invent agents.
_CONJUNCTION = re.compile(r'\s(?:and|og|och|und|et|y|e|en)\s', re.IGNORECASE)
_ET_AL = re.compile(r'\bet\.?\s*al\b', re.IGNORECASE)
# Name forms that suggest several agents, so one explicit ID cannot be paired with them.
MULTI_AGENT_REASONS = frozenset({'delimited_list', 'incomplete_group', 'conjunction', 'comma_list'})


@dataclass(frozen=True)
class RoleField:
    resource: str      # target table holding the mapped name, e.g. 'occurrence'
    name_field: str    # e.g. 'recordedBy'
    id_field: str      # e.g. 'recordedByID'
    subject_pk: str    # e.g. 'occurrence_pk'
    role_table: str    # e.g. 'occurrence-agent-role'
    subject_fk: str    # e.g. 'occurrence_fk'
    role: str          # agentRole value


def _role_fields() -> dict[tuple[str, str], RoleField]:
    """Every <table>.<x>By / <x>ByID pair whose table has a <table>-agent-role table."""
    found = {}
    for resource, spec in TABLE_SPECS.items():
        role_table = f'{resource}-agent-role'
        if role_table not in TABLE_SPECS or len(spec.primary_key) != 1:
            continue
        subject_fk = next(key['fields'] for key in TABLE_SPECS[role_table].foreign_keys
                          if key['reference']['resource'] == resource)
        for name in spec.fields:
            if name.endswith('By') and name + 'ID' in spec.fields:
                found[(resource, name)] = RoleField(resource, name, name + 'ID', spec.primary_key[0],
                                                    role_table, subject_fk, name)
    return found


ROLE_FIELDS = _role_fields()


@dataclass(frozen=True)
class EmittedRow:
    table: str
    row: dict
    # (target resource, 1-based row number in that resource, name field) for each mention.
    mentions: tuple[tuple[str, int, str], ...]


@dataclass
class AgentRoleResult:
    rows: list[EmittedRow] = field(default_factory=list)
    report: dict = field(default_factory=dict)

    def tables(self) -> dict[str, list[dict]]:
        grouped = defaultdict(list)
        for emitted in self.rows:
            grouped[emitted.table].append(emitted.row)
        return dict(grouped)


def composite_name_reason(value: str) -> str | None:
    """Why a name value is not safely one agent, or None. Detection only; nothing is split."""
    text = value.strip()
    if not text:
        return 'empty'
    if text.casefold() in PLACEHOLDERS:
        return 'placeholder'
    if not any(character.isalpha() for character in text):
        return 'not_a_name'
    if any(separator in text for separator in ('|', ';', '/', '\\', '\n', '\t')):
        return 'delimited_list'
    if _ET_AL.search(text):
        return 'incomplete_group'
    if '&' in text or '+' in text or _CONJUNCTION.search(text):
        return 'conjunction'
    commas = text.count(',')
    if commas > 1:
        return 'comma_list'
    if commas == 1:
        # "Smith, J. R." and "John Smith, Jr." are one name; "John Smith, Kari Nordmann" is not.
        before, after = (part.strip() for part in text.split(','))
        if not before or not after or (' ' in before and ' ' in after):
            return 'comma_list'
    return None


def _variant_key(name: str) -> str:
    """Report-only grouping of spellings that differ by case, accents, punctuation or word order."""
    text = unicodedata.normalize('NFKD', name)
    text = ''.join(character for character in text if not unicodedata.combining(character)).casefold()
    return ' '.join(sorted(re.findall(r'\w+', text)))


def _records(rows) -> list[dict]:
    if hasattr(rows, 'to_dict'):
        return rows.fillna('').to_dict('records')
    return list(rows or [])


def _text(value) -> str:
    return '' if value is None else str(value).strip()


def agent_name(value) -> str:
    """The exact name that identifies a name-only agent: the text with whitespace runs collapsed."""
    return ' '.join(_text(value).split())


def paired_list(name: str, identifier: str, is_agent_identifier: Callable[[str], bool]) -> list[tuple[str, str]] | None:
    """(name, IRI) pairs when |-delimited names and IDs match one-to-one and each part is safe, else None."""
    if '|' not in name or '|' not in identifier:
        return None
    names = [agent_name(part) for part in name.split('|')]
    identifiers = [part.strip() for part in identifier.split('|')]
    if len(names) != len(identifiers) or len(set(identifiers)) != len(identifiers):
        return None
    if any(composite_name_reason(part) for part in names) or not all(map(is_agent_identifier, identifiers)):
        return None
    return list(zip(names, identifiers))


def build_agent_roles(
    resources: Mapping[str, object],
    key: Callable[..., str],
    *,
    shared_names: Iterable[str] = (),
    fields: Iterable[tuple[str, str]] | None = None,
    is_agent_identifier: Callable[[str], bool] | None = None,
    example_limit: int = EXAMPLE_LIMIT,
) -> AgentRoleResult:
    """Return new agent and role rows for mapped records, and a report.

    ``resources`` maps resource names to lists of row dicts (or DataFrames). Existing
    ``agent`` rows are reused by agentID or agent_pk; existing role rows are not
    repeated and their agentRoleOrder values are continued. ``key(*parts)`` must be
    deterministic; with the converter's ``_key`` an explicit-ID agent gets the same
    agent_pk as the converter's own ``key('agent', identifier)``. Nothing in
    ``resources`` is modified.

    ``shared_names`` lists exact name strings that review confirmed each denote
    a single agent, compared after ``agent_name`` normalisation. Other name-only
    mentions get no Agent or role rows. ``fields`` limits the (resource, name
    field) pairs processed; by default all pairs in ROLE_FIELDS present in
    ``resources`` are processed.
    """
    if is_agent_identifier is None:
        from api.dwca_conversion import _single_agent_iri as is_agent_identifier
    shared = {agent_name(name) for name in shared_names if agent_name(name)}
    selected = sorted(ROLE_FIELDS if fields is None else fields)
    unknown = [pair for pair in selected if pair not in ROLE_FIELDS]
    if unknown:
        raise ValueError(f'No agent role table for {unknown}.')

    agent_by_id = {}
    agent_keys = set()
    for row in _records(resources.get('agent')):
        agent_keys.add(_text(row.get('agent_pk')))
        if _text(row.get('agentID')):
            agent_by_id.setdefault(_text(row['agentID']), _text(row['agent_pk']))
    linked = defaultdict(set)
    next_order = defaultdict(lambda: 1)
    for role_table in {ROLE_FIELDS[pair].role_table for pair in selected}:
        subject_fk = next(item.subject_fk for item in ROLE_FIELDS.values() if item.role_table == role_table)
        for row in _records(resources.get(role_table)):
            group = (role_table, _text(row.get(subject_fk)), _text(row.get('agentRole')),
                     _text(row.get('agentRoleIRI')), _text(row.get('agentRoleSource')))
            linked[group].add(_text(row.get('agent_fk')))
            order = _text(row.get('agentRoleOrder'))
            if order.isdigit():
                next_order[group] = max(next_order[group], int(order) + 1)

    new_agents = {}          # agent_pk -> {'row', 'mentions', 'basis'}
    id_names = defaultdict(set)
    role_rows = []
    field_stats = {}
    skipped = Counter()
    skipped_values = {}
    name_mentions = defaultdict(list)    # safe, unconfirmed name-only mentions
    safe_names = set()
    used_shared = set()
    reused_ids = set()
    already_linked = 0

    def new_agent(agent_pk, row, mention, basis):
        if agent_pk in agent_keys:
            return
        entry = new_agents.setdefault(agent_pk, {'row': row, 'mentions': [], 'basis': basis})
        entry['mentions'].append(mention)

    for pair in selected:
        spec = ROLE_FIELDS[pair]
        rows = _records(resources.get(spec.resource))
        if not rows:
            continue
        stats = field_stats[f'{spec.resource}.{spec.name_field}'] = {
            'mentions': 0, 'linked_by_id': 0, 'linked_by_id_list': 0, 'unlinked_name_only': 0, 'shared_name': 0,
            'skipped': Counter()}
        for number, record in enumerate(rows, start=1):
            name, identifier = _text(record.get(spec.name_field)), _text(record.get(spec.id_field))
            if not name and not identifier:
                continue
            stats['mentions'] += 1
            mention = (spec.resource, number, spec.name_field)
            subject = _text(record.get(spec.subject_pk))
            reason = None
            pairs = None
            if not subject:
                reason = 'missing_subject_key'
            elif identifier:
                pairs = paired_list(name, identifier, is_agent_identifier)
                if pairs:
                    stats['linked_by_id_list'] += 1
                elif not is_agent_identifier(identifier):
                    reason = 'id_not_single_iri'
                elif composite_name_reason(name) in MULTI_AGENT_REASONS:
                    reason = 'id_name_count_mismatch'
            else:
                reason = composite_name_reason(name)
            if reason:
                stats['skipped'][reason] += 1
                skipped[reason] += 1
                example = skipped_values.setdefault((spec.resource, spec.name_field, name, identifier, reason),
                                                    {'resource': spec.resource, 'field': spec.name_field, 'value': name,
                                                     'id': identifier, 'reason': reason, 'rows': 0, 'first_row': number})
                example['rows'] += 1
                continue
            agent_pks = []
            for part_name, part_id in pairs or [(name, identifier)]:
                if part_id:
                    agent_pk = agent_by_id.get(part_id)
                    if agent_pk:
                        reused_ids.add(part_id)
                    else:
                        agent_pk = key('agent', part_id)
                        new_agent(agent_pk, {'agent_pk': agent_pk, 'agentID': part_id, 'preferredAgentName': ''},
                                  mention, 'explicit_id')
                    if part_name and not composite_name_reason(part_name):
                        id_names[part_id].add(agent_name(part_name))
                    if not pairs:
                        stats['linked_by_id'] += 1
                else:
                    exact = agent_name(part_name)
                    safe_names.add(exact)
                    if exact not in shared:
                        name_mentions[exact].append(mention)
                        stats['unlinked_name_only'] += 1
                        continue
                    agent_pk = key('agent-name', exact)
                    new_agent(agent_pk, {'agent_pk': agent_pk, 'preferredAgentName': exact,
                                         'agentRemarks': SHARED_NAME_REMARK}, mention, 'shared_name')
                    used_shared.add(exact)
                    stats['shared_name'] += 1
                agent_pks.append(agent_pk)
            group = (spec.role_table, subject, spec.role, '', '')
            for agent_pk in agent_pks:
                if agent_pk in linked[group]:
                    already_linked += 1
                    continue
                linked[group].add(agent_pk)
                role_rows.append(EmittedRow(spec.role_table, {
                    spec.subject_fk: subject, 'agent_fk': agent_pk, 'agentRole': spec.role,
                    'agentRoleOrder': next_order[group]}, (mention,)))
                next_order[group] += 1

    # As in the converter, a paired name is evidence for an explicit ID only when it is unique.
    for entry in new_agents.values():
        identifier = entry['row'].get('agentID')
        if identifier and len(id_names[identifier]) == 1:
            entry['row']['preferredAgentName'] = next(iter(id_names[identifier]))

    result = AgentRoleResult()
    result.rows = [EmittedRow('agent', entry['row'], tuple(entry['mentions'])) for entry in new_agents.values()]
    result.rows.extend(role_rows)

    repeated = sorted(((name, mentions) for name, mentions in name_mentions.items() if len(mentions) > 1),
                      key=lambda item: (-len(item[1]), item[0]))
    variants = defaultdict(set)
    for name in safe_names:
        variants[_variant_key(name)].add(name)
    variant_groups = sorted((sorted(names) for names in variants.values() if len(names) > 1),
                            key=lambda names: (-len(names), names))
    examples = sorted(skipped_values.values(), key=lambda item: (-item['rows'], item['resource'], item['field'], item['value']))
    bases = Counter(entry['basis'] for entry in new_agents.values())
    result.report = {
        'policy': POLICY,
        'agents_created': {'shared_name': bases['shared_name'], 'explicit_id': bases['explicit_id']},
        'unlinked_name_only': sum(len(mentions) for mentions in name_mentions.values()),
        'explicit_id_agents_reused': len(reused_ids),
        'roles_created': dict(sorted(Counter(row.table for row in role_rows).items())),
        'roles_already_present': already_linked,
        'fields': {name: {**stats, 'skipped': dict(sorted(stats['skipped'].items()))}
                   for name, stats in sorted(field_stats.items())},
        'skipped': dict(sorted(skipped.items())),
        'skipped_values': examples[:example_limit],
        'skipped_values_omitted': max(0, len(examples) - example_limit),
        # Repeated unlinked names are candidates for an explicit shared_names decision.
        'repeated_names': [{'name': name, 'mentions': len(mentions),
                            'fields': sorted({f'{resource}.{name_field}' for resource, _number, name_field in mentions})}
                           for name, mentions in repeated[:example_limit]],
        'repeated_names_omitted': max(0, len(repeated) - example_limit),
        # Possible spelling variants; never merged automatically.
        'variant_groups': variant_groups[:example_limit],
        'variant_groups_omitted': max(0, len(variant_groups) - example_limit),
        'shared_names_unused': sorted(shared - used_shared),
    }
    return result
