"""Deterministic, bounded evidence for the conversion AI reviewer and chat.

See docs/dwca-conversion/ai-review-and-chat.md §3, §4 and §8.1. Everything here is
a pure function of the plan, the decisions and (for packets) the source archive.
Source text is untrusted data; it is cleaned and truncated, never interpreted.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import PurePosixPath

from lxml import etree

from api.dwca_review import effective_decisions, option_status

LEVELS = {
    'layout': 0, 'taxonomy-package': 0,
    'event-grain': 1, 'event-category': 1, 'occurrence-status': 1, 'extension-role': 1, 'taxon-occurrences': 1,
    'material-identity': 2, 'survey-classification': 2, 'occurrence-events': 2,
    'column-mapping': 3, 'name-semantics': 3, 'external-identifier': 3, 'trait-link': 3, 'row-handling': 3,
    'survey-completeness': 3,
}
ROOT_DECISIONS = ('loose-links', 'taxonomy-package')
# Nested Taxon occurrence items sit below every top-level item, so their outer role is always a lower level.
NESTED_OFFSET = 4
MAX_LEVEL = 3 + NESTED_OFFSET
PACKET_LIMIT = 8000
EML_LIMIT = 8000
EML_MAX_BYTES = 2 * 1024 * 1024
PUBLICATION_EML_MAX_BYTES = 64 * 1024 * 1024  # parsed without entities, network or huge-tree text nodes
TOP_VALUES = 10
ROW_LIMIT = 5
ROW_COLUMNS = 20
NESTED = re.compile(r'^(taxon-occurrence:\d+:)(.*)$')
_CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
KIND_TERMS = {
    'occurrence-status': ('occurrenceStatus', 'organismQuantity', 'organismQuantityType', 'individualCount',
                          'basisOfRecord', 'samplingProtocol'),
    'material-identity': ('materialSampleID', 'materialEntityID', 'catalogNumber', 'basisOfRecord', 'preparations',
                          'occurrenceID'),
    'event-category': ('eventID', 'eventCategory', 'eventType', 'samplingProtocol', 'parentEventID'),
    'event-grain': ('eventID',),
    'occurrence-events': ('eventID', 'eventDate', 'year', 'decimalLatitude', 'decimalLongitude', 'locality'),
}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), default=str)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def clip(value, limit):
    text = ' '.join(_CONTROL.sub('', str(value)).split())
    return text if len(text) <= limit else text[:max(limit - 1, 0)] + '…'


# EML ---------------------------------------------------------------------------

def _parser():
    return etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)


def _metadata_name(files):
    """(declared, path): the meta.xml metadata attribute, resolved; a declaration wins over any eml.xml."""
    metas = [name for name in files if PurePosixPath(name).name.lower() == 'meta.xml']
    if len(metas) != 1:
        return False, None
    try:
        root = etree.fromstring(files[metas[0]], parser=_parser())
    except (etree.XMLSyntaxError, ValueError):
        return False, None
    declared = root.get('metadata')
    if not declared:
        return False, None
    candidate = (PurePosixPath(metas[0]).parent / declared).as_posix()
    return True, candidate if candidate in files else None


def _text(element):
    return ' '.join(' '.join(element.itertext()).split())


def _all(root, path):
    steps = '/'.join(f'*[local-name()="{step}"]' for step in path.split('/'))
    return root.xpath(f'.//{steps}')


def _eml_dataset(archive, max_bytes=EML_MAX_BYTES):
    """(dataset element, None) for the single declared or supplied EML document, or (None, reason)."""
    files = archive.files
    declares, declared = _metadata_name(files)
    if declares and declared is None:
        return None, 'meta.xml names a metadata document that was not supplied.'
    candidates = [declared] if declared else sorted(name for name in files if PurePosixPath(name).name.lower() == 'eml.xml')
    if len(candidates) != 1:
        return None, 'No metadata document was supplied.' if not candidates else 'Several metadata documents were supplied.'
    content = files[candidates[0]]
    if len(content) > max_bytes:
        return None, 'The metadata document is too large to summarise.'
    try:
        root = etree.fromstring(content, parser=_parser())
    except (etree.XMLSyntaxError, ValueError) as exc:
        return None, f'The metadata document could not be read ({type(exc).__name__}).'
    datasets = _all(root, 'dataset')
    if not datasets:
        return None, 'The metadata document has no dataset section.'
    return datasets[0], None


def publication_metadata(archive, limit):
    """The EML title and abstract in full for dataset fields, with whether either exceeded the field limit."""
    # The evidence limit keeps model prompts small; dataset fields may come from any EML an upload can hold.
    dataset, _ = _eml_dataset(archive, max_bytes=PUBLICATION_EML_MAX_BYTES)
    if dataset is None:
        return {'title': '', 'description': '', 'truncated': {}}
    title = next((text for element in _all(dataset, 'title') if (text := _text(element))), '')
    paragraphs = [text for abstract in _all(dataset, 'abstract')[:1]
                  for text in ([_text(para) for para in _all(abstract, 'para')] or [_text(abstract)]) if text]
    description = '\n\n'.join(paragraphs)
    return {'title': title[:limit], 'description': description[:limit],
            'truncated': {key: True for key, value in (('title', title), ('description', description)) if len(value) > limit}}


def extract_eml(archive):
    """The bounded EML sections from §4.1, or {'available': False, 'reason': ...}."""
    dataset, reason = _eml_dataset(archive)
    if dataset is None:
        return {'available': False, 'reason': reason}

    def joined(path, limit, count=None):
        values = [_text(element) for element in _all(dataset, path)]
        values = [value for value in values if value][:count]
        return clip(' | '.join(values), limit) if values else ''

    boxes = []
    for box in _all(dataset, 'coverage/geographicCoverage/boundingCoordinates'):
        bounds = {side: _text(element) for side in ('westBoundingCoordinate', 'eastBoundingCoordinate',
                                                     'northBoundingCoordinate', 'southBoundingCoordinate')
                  for element in _all(box, side)[:1]}
        if bounds:
            boxes.append(', '.join(f'{side.replace("BoundingCoordinate", "")} {value}' for side, value in bounds.items()))
    taxa = []
    for classification in _all(dataset, 'coverage/taxonomicCoverage/taxonomicClassification')[:20]:
        name = next((_text(element) for element in _all(classification, 'taxonRankValue')[:1]), '')
        rank = next((_text(element) for element in _all(classification, 'taxonRankName')[:1]), '')
        if name:
            taxa.append(f'{name} ({rank})' if rank else name)
    sections = {
        'eml:title': joined('title', 300, 1),
        'eml:abstract': joined('abstract', 2000),
        'eml:purpose': joined('purpose', 600),
        'eml:keywords': joined('keywordSet/keyword', 400, 20),
        'eml:methods': clip(' | '.join(_text(element) for element in _all(dataset, 'methods/methodStep/description')
                                       if _text(element)), 2500),
        'eml:sampling': clip(' | '.join(filter(None, (joined('methods/sampling/studyExtent', 1500),
                                                      joined('methods/sampling/samplingDescription', 1500)))), 1500),
        'eml:quality': joined('methods/qualityControl', 600),
        'eml:coverage-geographic': clip(' | '.join(filter(None, (joined('coverage/geographicCoverage/geographicDescription', 600),
                                                                 *boxes))), 600),
        'eml:coverage-temporal': clip(' | '.join(_text(element) for element in _all(dataset, 'coverage/temporalCoverage')
                                                 if _text(element)), 200),
        'eml:coverage-taxonomic': clip(' | '.join(filter(None, (joined('coverage/taxonomicCoverage/generalTaxonomicCoverage', 400),
                                                                ', '.join(taxa)))), 800),
    }
    kept, used = {}, 0
    for ref, text in sections.items():
        if not text:
            continue
        text = text[:max(EML_LIMIT - used, 0)]
        if not text:
            break
        kept[ref] = text
        used += len(text)
    return {'available': True, 'sections': kept}


# Items, levels and dependencies -------------------------------------------------

def entries(plan):
    return {entry['id']: entry for entry in [*plan.get('issues', []), *plan.get('automatic_choices', [])]}


def issue_index(plan):
    return {issue['id']: issue for issue in plan.get('issues', [])}


def level(entry):
    return LEVELS.get(entry.get('kind'), 3) + (NESTED_OFFSET if NESTED.match(entry.get('id', '')) else 0)


def split_nested(item_id):
    match = NESTED.match(item_id)
    return (match.group(1), match.group(2)) if match else ('', item_id)


def requirements_for(plan, item_id):
    """{value: [requirement]} for an item, looking inside nested Taxon occurrence plans."""
    prefix, local = split_nested(item_id)
    if prefix:
        inner = plan.get('taxonomy', {}).get('occurrence_plans', {}).get(prefix.split(':')[1], {})
        return inner.get('requirements', {}).get(local, {})
    return plan.get('requirements', {}).get(item_id, {})


def _decision_refs(condition):
    if condition['type'] in {'any', 'all'}:
        return [identifier for inner in condition['conditions'] for identifier in _decision_refs(inner)]
    return [condition['id']] if condition['type'] == 'decision_in' else []


def dependencies(plan, item_id):
    """Judgment dependencies, always at a strictly lower level (§8.1). Group members listed last."""
    known = entries(plan)
    item = known.get(item_id)
    if item is None:
        return []
    mine = level(item)
    prefix, _ = split_nested(item_id)
    found = []

    def add(identifier):
        entry = known.get(identifier)
        if entry is not None and identifier != item_id and level(entry) < mine and identifier not in found:
            found.append(identifier)

    for root in ROOT_DECISIONS:
        add(root)
        if prefix:
            add(prefix + root)
    if 'table' in item and (mine > 1 or prefix):
        add(f"table:{item['table']}")
    if item.get('kind') == 'material-identity':
        add(prefix + 'event-grain')
    if item.get('kind') == 'survey-completeness' and 'table' in item:
        add(f"hum-category:{item['table']}")
        if prefix:
            for identifier in known:
                if identifier.startswith(prefix + 'hum-category:'):
                    add(identifier)
    for requirements in requirements_for(plan, item_id).values():
        for requirement in requirements:
            for condition in [*requirement.get('when', []), *requirement['conditions']]:
                for identifier in _decision_refs(condition):
                    add(prefix + identifier if prefix and not identifier.startswith('taxon-occurrence:') else identifier)
    return found


def basis(plan, decisions, item_id, effective=None):
    effective = effective if effective is not None else effective_decisions(plan, decisions)
    values = {identifier: effective.get(identifier) for identifier in dependencies(plan, item_id)}
    for member in issue_index(plan).get(item_id, {}).get('members') or []:
        values[member] = decisions.get(member)
    return values


def availability(plan, decisions, item_id, status=None):
    status = status if status is not None else option_status(plan, decisions)
    item = entries(plan).get(item_id, {})
    return {option['value']: bool(status.get(item_id, {}).get(option['value'], {}).get('available', True))
            for option in item.get('options', [])}


def deferred_by(plan, decisions, item_id, effective=None):
    """The first unresolved dependency (excluding group members), or None."""
    effective = effective if effective is not None else effective_decisions(plan, decisions)
    return next((identifier for identifier in dependencies(plan, item_id) if effective.get(identifier) is None), None)


# Packets ------------------------------------------------------------------------

def _short(term):
    return term.rsplit('/', 1)[-1].rsplit('#', 1)[-1]


def item_table(plan, item):
    """The source table an item is about: its own, the index in its id, or the core for global items."""
    if isinstance(item.get('table'), int):
        return item['table']
    _, local = split_nested(item['id'])
    tail = local.rsplit(':', 1)[-1]
    if ':' in local and tail.isdigit():
        return int(tail)
    return next((index for index, table in enumerate(plan.get('tables', [])) if table.get('core')), None)


def _source_table(archive, plan, item):
    """The archive table rows for an item; nested Taxon items read their occurrence table."""
    t = item_table(plan, item)
    if archive is None or t is None:
        return None
    return archive.tables[t] if 0 <= t < len(archive.tables) else None


def _profile(table, c, column):
    values = [row[c] for row in table.rows if c < len(row) and row[c] != ''] if table is not None else []
    counts = Counter(values)
    order = {value: index for index, value in enumerate(dict.fromkeys(values))}
    top = sorted(counts.items(), key=lambda item: (-item[1], order[item[0]]))[:TOP_VALUES]
    return {'ref': f"col:{column['table']}:{c}", 'term': column['term'], 'header': _short(column['term']),
            'nonempty': column.get('nonempty', len(values)), 'distinct': column.get('distinct', len(counts)),
            'top_values': [[clip(value, 120), count] for value, count in top] if table is not None
            else [[clip(value, 120), None] for value in column.get('samples', [])[:3]]}


def _columns_for(plan, item, table):
    t = item_table(plan, item)
    if t is None:
        return []
    columns = [column for column in plan.get('columns', []) if column['table'] == t
               and split_nested(column['id'])[0] == split_nested(item['id'])[0]]
    kind = item.get('kind')
    by_id = {column['id']: column for column in columns}
    chosen = []
    if item['id'] in by_id:
        chosen.append(by_id[item['id']])
        chosen.extend(column for column in columns if _short(column['term']) in {'occurrenceID', 'eventID', 'taxonID', 'id'}
                      and column['id'] != item['id'])
    elif kind in KIND_TERMS:
        chosen.extend(column for column in columns if _short(column['term']) in KIND_TERMS[kind])
    elif kind in {'survey-classification', 'survey-completeness'}:
        chosen.extend(column for column in columns if any(word in _short(column['term']).lower()
                                                          for word in ('scope', 'complete', 'survey', 'target', 'event')))
    elif kind == 'layout':
        chosen.extend(column for column in plan.get('columns', []) if _short(column['term']) in {'id', 'coreid', 'occurrenceID', 'eventID', 'taxonID'})
    elif kind == 'row-handling' and table is not None:
        rows = item.get('rows') or ([item['row']] if 'row' in item else [])
        populated = {column['column'] for column in columns for n in rows[:ROW_LIMIT]
                     if 0 < n <= len(table.rows) and column['column'] < len(table.rows[n - 1]) and table.rows[n - 1][column['column']]}
        chosen.extend(column for column in columns if column['column'] in populated)
    for requirement_list in requirements_for(plan, item['id']).values():
        for requirement in requirement_list:
            for condition in [*requirement.get('when', []), *requirement['conditions']]:
                stack = [condition]
                while stack:
                    current = stack.pop()
                    if current['type'] in {'any', 'all'}:
                        stack.extend(current['conditions'])
                    elif current.get('column') in by_id:
                        chosen.append(by_id[current['column']])
    if not chosen:
        limit = 15 if kind in {'extension-role', 'taxon-occurrences'} else 12
        chosen = sorted(columns, key=lambda column: (-column.get('nonempty', 0), column['column']))[:limit]
    return list({column['id']: column for column in chosen}.values())[:15]


def _rows_for(item, table, requirement_rows):
    if table is None or not table.rows:
        return []
    if item.get('rows'):
        numbers = item.get('sample_rows') or item['rows'][:ROW_LIMIT]
    elif 'row' in item:
        numbers = [item['row']]
    elif requirement_rows:
        numbers = requirement_rows
    else:
        size = len(table.rows)
        numbers = list(dict.fromkeys(min(size, max(1, 1 + (size - 1) * k // 4)) for k in range(5)))
    return [n for n in dict.fromkeys(numbers) if isinstance(n, int) and 0 < n <= len(table.rows)][:ROW_LIMIT]


def _bounded(value, limit):
    """Structured evidence when small enough, otherwise a clipped canonical string."""
    text = canonical(value)
    return value if len(text) <= limit else clip(text, limit)


def _example_rows(evidence):
    found = []
    stack = [evidence]
    while stack and len(found) < ROW_LIMIT:
        current = stack.pop(0)
        if isinstance(current, dict):
            for key, value in current.items():
                if key in {'row', 'source_row'} and isinstance(value, int):
                    found.append(value)
                elif key == 'rows' and isinstance(value, list):
                    found.extend(row for row in value if isinstance(row, int))
                else:
                    stack.append(value)
        elif isinstance(current, list):
            stack.extend(current)
    return found[:ROW_LIMIT]


def _targets(item):
    from api.dwc_dp_specs import TABLE_SPECS
    found = []
    for option in item.get('options', []) + item.get('unavailable_options', []):
        value = option['value']
        if '.' not in value:
            continue
        name, field = value.split('.', 1)
        spec = TABLE_SPECS.get(name)
        if spec is None or field not in spec.field_descriptors:
            continue
        definition = spec.field_descriptors[field]
        examples = definition.get('examples') or []
        found.append({'ref': f'target:{value}', 'table_meaning': clip(spec.description, 300),
                      'field_meaning': clip(definition.get('description', ''), 500),
                      'comments': clip(definition.get('comments', ''), 300),
                      'examples': [clip(example, 120) for example in (examples if isinstance(examples, list) else [examples])[:3]]})
    return found[:8]


def _source_of(identifier, sources, plan, decisions):
    if identifier in decisions:
        return sources.get(identifier, 'user')
    if identifier in {choice['id'] for choice in plan.get('automatic_choices', [])}:
        return 'automatic'
    return 'unresolved'


def evidence_packet(plan, archive, decisions, item_id, *, status=None, sources=None, effective=None):
    """(packet, excerpts) for one review item. excerpts maps each ref to a ≤300-character fact."""
    item = issue_index(plan)[item_id]
    status = status if status is not None else option_status(plan, decisions)
    effective = effective if effective is not None else effective_decisions(plan, decisions)
    sources = sources or {}
    table = _source_table(archive, plan, item)
    requirements = []
    requirement_rows = []
    for value, requirement_list in requirements_for(plan, item_id).items():
        available = status.get(item_id, {}).get(value, {}).get('available', True)
        for index, requirement in enumerate(requirement_list):
            evidence = requirement.get('evidence', {})
            requirement_rows.extend(_example_rows(evidence))
            requirements.append({'ref': f'req:{value}:{index}', 'option': value, 'satisfied': available,
                                 'reason': clip(requirement['reason'], 400), 'evidence': _bounded(evidence, 1500)})
    options = [{'value': option['value'], 'label': option['label'], 'assertion': bool(option.get('assertion')),
                'available': bool(status.get(item_id, {}).get(option['value'], {}).get('available', True)),
                'reasons': [clip(reason, 300) for reason in status.get(item_id, {}).get(option['value'], {}).get('reasons', [])]}
               for option in item['options']]
    options += [{'value': option['value'], 'label': option['label'], 'assertion': bool(option.get('assertion')),
                 'available': False, 'reasons': [clip(option.get('reason', ''), 300)]}
                for option in item.get('unavailable_options', [])]
    evidence = {}
    profiles = plan.get('tables', [])
    t = item_table(plan, item)
    if t is not None and 0 <= t < len(profiles):
        profile = profiles[t]
        evidence['table'] = {'ref': f"table:{t}", 'name': clip(profile['name'], 200),
                             'row_type': profile.get('row_type', ''), 'core': profile.get('core', False),
                             'rows': profile.get('rows'), 'join_basis': clip(profile.get('join_basis', ''), 200)}
    if item.get('kind') in {'layout', 'taxonomy-package'}:
        evidence['tables'] = [{'ref': f'table:{index}', 'name': clip(profile['name'], 200), 'row_type': profile.get('row_type', ''),
                               'core': profile.get('core', False), 'rows': profile.get('rows'),
                               'unique_join_ids': profile.get('unique_join_ids'), 'join_basis': clip(profile.get('join_basis', ''), 200)}
                              for index, profile in enumerate(profiles[:12])]
    columns = _columns_for(plan, item, table)
    evidence['columns'] = [_profile(table, column['column'], column) for column in columns]
    row_columns = [column for column in columns if table is not None]
    if table is not None and item.get('kind') in {'row-handling', 'survey-completeness'}:
        row_columns = [column for column in plan.get('columns', []) if column['table'] == t
                       and split_nested(column['id'])[0] == split_nested(item_id)[0]]
    rows = []
    for n in _rows_for(item, table, requirement_rows):
        values = {}
        for column in row_columns:
            c = column['column']
            if c < len(table.rows[n - 1]) and table.rows[n - 1][c] != '' and len(values) < ROW_COLUMNS:
                values[_short(column['term'])] = clip(table.rows[n - 1][c], 200)
        rows.append({'ref': f"row:{t}:{n}", 'row': n, 'values': values})
    evidence['rows'] = rows
    evidence['requirements'] = requirements
    if item.get('members'):
        exceptions = {member: decisions[member] for member in item['members'] if member in decisions}
        evidence['group'] = {'ref': 'group', 'count': item.get('count', len(item['members'])), 'rows': item.get('rows', [])[:20],
                             'rows_total': len(item.get('rows', [])),
                             'shared': _bounded(item.get('scope_values') or {}, 1000),
                             'exceptions': dict(list(exceptions.items())[:20])}
    evidence['targets'] = _targets(item)
    evidence['context'] = [{'ref': f'decision:{identifier}', 'id': identifier, 'value': effective.get(identifier),
                            'title': clip(entries(plan).get(identifier, {}).get('title', identifier), 150),
                            'source': _source_of(identifier, sources, plan, decisions) if effective.get(identifier) is not None
                            else 'unresolved'}
                           for identifier in dependencies(plan, item_id)]
    packet = {'id': item_id, 'kind': item.get('kind'), 'authority': item.get('authority'),
              'title': clip(item.get('title', ''), 300), 'reason': clip(item.get('reason', ''), 800),
              'options': options, 'evidence': evidence}
    _fit(packet)
    return packet, excerpts(packet)


def _fit(packet):
    """Trim in a fixed order until the packet fits PACKET_LIMIT characters."""
    evidence = packet['evidence']
    steps = [
        lambda: evidence.__setitem__('rows', evidence['rows'][:3]),
        lambda: [column.__setitem__('top_values', column['top_values'][:5]) for column in evidence['columns']],
        lambda: [target.__setitem__('comments', '') for target in evidence['targets']],
        lambda: evidence.__setitem__('columns', evidence['columns'][:8]),
        lambda: [requirement.__setitem__('evidence', clip(canonical(requirement['evidence']), 300))
                 for requirement in evidence['requirements']],
        lambda: evidence.__setitem__('rows', evidence['rows'][:1]),
        lambda: evidence.__setitem__('targets', evidence['targets'][:3]),
        lambda: [row.__setitem__('values', dict(list(row['values'].items())[:8])) for row in evidence['rows']],
    ]
    final = [
        lambda: evidence.__setitem__('requirements', evidence['requirements'][:4]),
        lambda: [option.__setitem__('reasons', [clip(' '.join(option['reasons']), 150)] if option['reasons'] else [])
                 for option in packet['options']],
        lambda: evidence.__setitem__('context', evidence['context'][:6]),
        lambda: [requirement.__setitem__('evidence', '') for requirement in evidence['requirements']],
        lambda: evidence.__setitem__('columns', evidence['columns'][:3]),
        lambda: evidence.pop('tables', None),
        lambda: evidence.__setitem__('rows', []),
        lambda: evidence.__setitem__('targets', []),
        lambda: [option.__setitem__('label', clip(option['label'], 80)) or option.__setitem__('reasons', [])
                 for option in packet['options']],
        lambda: packet.__setitem__('options', packet['options'][:40]),
    ]
    for step in [*steps, *final]:
        if len(canonical(packet)) <= PACKET_LIMIT:
            return packet
        step()
    if len(canonical(packet)) > PACKET_LIMIT:
        raise ValueError(f"Evidence for {packet['id']} cannot fit the packet limit.")
    return packet


def excerpts(packet):
    """{ref: short fact} for every citable fact in a packet."""
    found = {}
    evidence = packet['evidence']
    for table in [*([evidence['table']] if 'table' in evidence else []), *evidence.get('tables', [])]:
        found[table['ref']] = clip(f"Table {table['name']}: {table['rows']} rows, {_short(table['row_type']) or 'unrecognised'}, "
                                   f"{'core' if table['core'] else 'extension'}; joined by {table['join_basis']}", 300)
    for column in evidence['columns']:
        values = ', '.join(f'{value} ({count})' if count is not None else value for value, count in column['top_values'][:5])
        found[column['ref']] = clip(f"{column['header']}: {column['nonempty']} values, {column['distinct']} distinct; {values}", 300)
    for row in evidence['rows']:
        found[row['ref']] = clip(f"Row {row['row']}: " + '; '.join(f'{key}={value}' for key, value in row['values'].items()), 300)
    for requirement in evidence['requirements']:
        found[requirement['ref']] = clip(f"{requirement['option']} {'satisfied' if requirement['satisfied'] else 'not satisfied'}: "
                                         f"{requirement['reason']}", 300)
    if 'group' in evidence:
        group = evidence['group']
        found['group'] = clip(f"{group['count']} rows with the same question: rows {', '.join(map(str, group['rows'][:10]))}", 300)
    for target in evidence['targets']:
        found[target['ref']] = clip(f"{target['ref'][7:]}: {target['field_meaning']}", 300)
    for context in evidence['context']:
        found[context['ref']] = clip(f"{context['title']}: {context['value'] or 'not decided'} ({context['source']})", 300)
    return found


def eml_excerpts(eml):
    return {ref: clip(text, 300) for ref, text in (eml.get('sections') or {}).items()}
