"""Versioned plans and deterministic conversion. No network calls in this module."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import Counter, defaultdict
from urllib.parse import urlsplit

import pandas as pd

from api.dwca_import import DWC, ConversionError, ImportFailure, REGISTRY, dropped_extension_warnings
from api.dwca_tidy import column_note, pending_suggestions
from api.dwca_media import MEDIA_FAMILIES, MEDIA_SUBJECT_TERMS, media_targets
from api.dwca_references import REFERENCE_FAMILIES, NON_EXACT_TARGETS, IDENTIFIER_ROW_TYPE, REFERENCE_ROW_TYPE, DC, reference_targets, emit_reference_records
from api.dwca_humboldt import HUMBOLDT_FAMILIES, IRI_DIRECT, DIRECT, blocked_fields, humboldt_targets, scope_review, emit_humboldt_records, valid_value
from api.dwca_eol import EOL_FAMILIES, EOL_MEDIA, EOL_REFERENCE, TEXT_TYPES, DCT, eol_targets, eol_media_row_review, emit_eol_records
from api.dwca_germplasm import (GERMPLASM_FAMILIES, GERMPLASM_DERIVED_TERMS, G, GEO,
                               germplasm_targets, emit_germplasm_records)
from api.dwca_hierarchy import resolve_parents, summary as hierarchy_summary, describe_problems, missing_reference
from api.dwca_scientific import audit_hierarchy
from api.dwca_legacy import (LEGACY_FAMILIES, LEGACY_DERIVED_TERMS, BMDE, NXF, GROUPS, UTM, TIMES, NBN_DATE,
                            legacy_targets, legacy_row_review, nbn_event_date, emit_legacy_records)
from api.dwc_dp_specs import TABLE_SPECS, dwc_dp_schema_snapshot, validate_dwc_dp_resources
from api.dwca_preflight import preflight
from api.dwca_review import (apply_policy, effective_decisions, failed_requirements, group_rows,  # noqa: F401
                             option_status, remove_unavailable, violations)
from api.dwca_semantic_audit import ASSERTION_IRI_FIELDS, is_age_like_remark, semantic_target_rejection
from api.dwca_agents import ROLE_FIELDS, agent_name, build_agent_roles, composite_name_reason, split_agent_ids

RULE_VERSION = "25"
DERIVED_VALUE_EXAMPLE_LIMIT = 20
ECO_SURVEY_ID = 'http://rs.tdwg.org/eco/terms/surveyID'
REGISTERED_TERMS = {term for terms in REGISTRY['terms'].values() for term in terms}
# Terms some field of the pinned DwC-DP schema is a version of; others have no Data Package field at all.
SCHEMA_TERMS = {field['dcterms:isVersionOf'] for spec in TABLE_SPECS.values() for field in spec.schema['fields']
                if field.get('dcterms:isVersionOf')}
NAME = DWC + 'scientificName'
AUTHORSHIP = DWC + 'scientificNameAuthorship'
VERBATIM_NAME = DWC + 'verbatimIdentification'
QUALIFIER = DWC + 'identificationQualifier'
SPECIMEN_BASES = {'PreservedSpecimen', 'FossilSpecimen', 'MaterialSample', 'LivingSpecimen', 'MaterialCitation'}
# Wording that may report an absence. Any match keeps occurrence status a question.
ABSENCE_WORDING = re.compile(r'\b(?:absent|absence|not (?:found|seen|observed|detected|present|recorded)|'
                             r'none (?:found|seen|observed)|no (?:individuals|specimens|organisms|catch)|'
                             r'ikke (?:funnet|observert|registrert|sett|påvist)|inte (?:hittad|observerad))\b', re.I)
OBIS = 'http://rs.iobis.org/obis/terms/'
# OBIS vocabulary identifiers are the IRI versions of the eMoF type, value and unit
# (the DwC-DP fields are versions of dwciri:measurementType etc.). Only absolute IRIs are copied.
OBIS_IRI_ALIASES = {'measurementTypeID': 'assertionTypeIRI', 'measurementValueID': 'assertionValueIRI',
                    'measurementUnitID': 'assertionUnitIRI'}
ROLE_DESCRIPTIONS = {
    'occurrence': 'Each row of {table} becomes an occurrence, linked to its event in {core}.',
    'identification': 'Each row of {table} becomes an identification of its linked occurrence in {core}.',
    'resource-relationship': 'Each row of {table} becomes a relationship between the records it names, keeping their identifiers.',
    'occurrence-assertion': 'Each row of {table} becomes a measurement or fact about its linked occurrence in {core}.',
    'event-assertion': 'Each row of {table} becomes a measurement or fact about its linked event in {core}.',
    'declared-assertions': 'Each row of {table} becomes a measurement or fact about the occurrence it names, or otherwise its linked event in {core}.',
    'identifier': 'Each row of {table} becomes an alternative identifier of its linked record in {core}.',
    'reference': 'Each row of {table} becomes a literature reference of its linked record in {core}.',
    'humboldt-survey': 'Each row of {table} becomes a survey description of its linked event in {core}.',
}
NAMESPACE = uuid.UUID("7750ccce-e9f9-4fd1-b9d8-a02a9747cae9")
PRESERVE = {"value": "preserve", "label": "Keep in original files only"}
PARENT = DWC + "parentEventID"
PARENT_LINK = "parent-link"
# event-grain value: events combined by eventID, each distinct depth of a group becoming a child event.
DEPTH_SPLIT = "by_id_depth"
DEPTH_FIELDS = ('verbatimDepth', 'minimumDepthInMeters', 'maximumDepthInMeters',
                'minimumDistanceAboveSurfaceInMeters', 'maximumDistanceAboveSurfaceInMeters')
COMBINED_GRAINS = ('by_id', DEPTH_SPLIT)
SUPPORTED_EXTENSIONS = {
    DWC + "Occurrence": "occurrence",
    DWC + "Identification": "identification",
    DWC + "MeasurementOrFact": "assertion",
    "http://rs.iobis.org/obis/terms/ExtendedMeasurementOrFact": "assertion",
    DWC + "ResourceRelationship": "relationship",
    "http://rs.gbif.org/terms/1.0/DNADerivedData": "molecular",
    **MEDIA_FAMILIES,
    **REFERENCE_FAMILIES,
    **HUMBOLDT_FAMILIES,
    **EOL_FAMILIES,
    **GERMPLASM_FAMILIES,
    **LEGACY_FAMILIES,
}


def _column_id(t, c):
    return f"column:{t}:{c}"


def _reviewed_value_id(kind, t, c, value):
    digest = hashlib.sha256(value.encode('utf-8')).hexdigest()
    return f'{kind}:{t}:{c}:{digest}'


AGENT_NAMES_ID = 'agent-names'


def _agent_names_warnings(warnings, roles):
    """The plan's notices, with the agent-names notice restated from the linked mentions (or dropped)."""
    linked = sum(stats.get('name', 0) for stats in roles['fields'].values())
    names = roles['agents_created'].get('name', 0)
    kept = roles.get('unlinked_name_only', 0)
    restated = []
    for warning in warnings:
        if warning.get('id') == AGENT_NAMES_ID:
            if not linked:
                continue
            warning = {**warning, 'count': linked, 'names': names, 'reason': (
                f"Linked {linked:,} {'mention' if linked == 1 else 'mentions'} of {names:,} "
                f"{'name' if names == 1 else 'names'} without identifiers to one agent per exact name"
                + (f"; {kept:,} {'mention was' if kept == 1 else 'mentions were'} kept as text only by your choices" if kept else '')
                + '. Names with identifiers are linked by their identifier.')}
        restated.append(warning)
    return restated


def _agent_name_id(value):
    return 'agent-share:' + hashlib.sha256(value.encode('utf-8')).hexdigest()


def _key(archive, *parts):
    # Internal keys are reproducible, never replacements for source identifiers.
    return str(uuid.uuid5(NAMESPACE, json.dumps([archive.fingerprint, *parts], ensure_ascii=False)))


def _candidates(term, tables):
    if term not in REGISTERED_TERMS:
        return []
    return [f"{name}.{field['name']}" for name in tables for field in TABLE_SPECS[name].schema["fields"]
            if field.get("dcterms:isVersionOf") == term and not field["name"].endswith(("_pk", "_fk"))
            and field['name'] != 'evidenceForOccurrenceID']


def _choice(target):
    if target == PARENT_LINK:
        return {"value": target, "label": "Link to the source event with this persistent eventID"}
    return {"value": target, "label": 'Use reviewed extension rules' if target == 'derive' else target.replace(".", " → ")}


def _typed_field(target):
    if '.' not in target:
        return None
    table, field = target.split('.', 1)
    descriptor = TABLE_SPECS[table].field_descriptors[field]
    return descriptor if descriptor.get('type') in {'integer', 'number', 'boolean'} else None


def _copy_rejection(row_type, target, value):
    """Reason a value cannot be copied to this target, or None."""
    from api.dwca_semantic_audit import semantic_target_rejection
    semantic_reason = semantic_target_rejection(target, value)
    if semantic_reason:
        return semantic_reason
    descriptor = _typed_field(target)
    if value and descriptor is not None and SUPPORTED_EXTENSIONS.get(row_type) != 'humboldt' and not valid_value(descriptor, value):
        return 'Value fails the approved target type or bounds; retained without normalization.'
    return None


def _copied(row_type, target, value):
    """Preflight's view of the value that would reach a mapped target."""
    return None if _copy_rejection(row_type, target, value) else value


def _year_disagrees(year, event_date):
    """A year outside its eventDate: a single date's year, or an interval's start and end years."""
    years = [int(found) for found in re.findall(r'(?<!\d)(\d{4})(?=-|/|$)', event_date or '')]
    if not year or not years:
        return False
    if '/' in event_date:
        return not min(years) <= int(year) <= max(years)
    return any(found != int(year) for found in years)


def _source(terms, row):
    source = {}
    for term, value in zip(terms, row):
        if value:
            if term in source and source[term] != value:
                raise ImportFailure(f'Conflicting source columns use the same term IRI: {term}.')
            source[term] = value
    return source


def _single_agent_iri(value):
    """An explicit single agent identifier, never a name or a list of IDs."""
    if not value or any(character.isspace() for character in value) or '|' in value:
        return False
    if re.search(r'[;,][A-Za-z][A-Za-z0-9+.-]*:', value) or value.count('://') > 1:
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    if parsed.scheme in {'http', 'https'}:
        return bool(parsed.netloc)
    return parsed.scheme in {'urn', 'did', 'mailto', 'tag'} and bool(parsed.path)


def _without_authorship(name, authorship):
    """Remove an exactly matching, separately supplied authorship suffix; DwC-DP names exclude authorship."""
    if name and authorship and name.endswith(' ' + authorship):
        return name[:-len(authorship) - 1].rstrip()
    return name


def _individual_count(source):
    """How a row's individualCount reaches the package: (route, count), route None when it cannot.

    'quantity': no supplied quantity, so the count becomes the occurrence's individuals quantity.
    'supplied': the supplied quantity already says the same number of individuals.
    'assertion': a different supplied quantity (a density, say) keeps the quantity pair, so the
    raw count becomes an individualCount assertion of the occurrence.
    """
    count = source.get(DWC + 'individualCount', '').strip()
    if not count:
        return None, 'empty'
    if not re.fullmatch(r'\+?\d+', count):
        return None, 'invalid'
    count = str(int(count))
    quantity = source.get(DWC + 'organismQuantity', '').strip()
    quantity_type = source.get(DWC + 'organismQuantityType', '').strip()
    if not quantity and not quantity_type:
        return 'quantity', count
    if quantity_type.casefold() == 'individuals' and re.fullmatch(r'\+?\d+', quantity) and int(quantity) == int(count):
        return 'supplied', count
    return 'assertion', count


def _words_at(text, words):
    """Start of `words` in `text` as whole whitespace-separated words, or None."""
    found = re.search(r'(?<!\S)' + re.escape(words) + r'(?!\S)', text)
    return found.start() if found else None


RANK_MARKERS = {'subsp.', 'ssp.', 'var.', 'subvar.', 'f.', 'fo.', 'forma', 'subf.', 'nothosubsp.', 'nothovar.', 'cv.', 'convar.'}
# Name-text words that stay with the name: open nomenclature written into scientificName.
NAME_QUALIFIER_WORDS = {'sp.', 'spp.', 'sp', 'spp', 'indet.', 'indet', '?', 'cf.', 'aff.', 'cf', 'aff'}
# Lowercase words that begin an author string ('de Vries'); a name containing one is not split.
AUTHOR_PARTICLES = {'de', 'del', 'della', 'der', 'den', 'des', 'di', 'du', 'la', 'le', 'van', 'von', 'zu', 'ex', 'in', 'et', 'non', 'sensu'}
# Lowercase words that are not epithets: groupings, concept and life-stage notes. A name containing one is not split.
NOT_EPITHETS = {'complex', 'group', 'grp', 'grp.', 'agg', 'agg.', 's.l.', 's.str.', 's.s.', 'lato', 'stricto', 'auct', 'auct.',
                'larva', 'larvae', 'juv', 'juv.', 'juvenile', 'juveniles', 'adult', 'nymph', 'pupa', 'egg', 'eggs',
                'nauplius', 'nauplii', 'copepodite', 'copepodites', 'nov.'}
HYBRID_MARKERS = {'x', '×'}


def _split_authorship(name, authorship=''):
    """(name words, authorship) of supplied name text, or None when it cannot be split safely.

    A supplied scientificNameAuthorship that ends the text is the authorship, and the rest
    must be name words. Otherwise the name words are a capitalised genus, an optional
    subgenus in parentheses ('Calanus (Calanus) finmarchicus'), then lowercase epithets,
    rank markers and open-nomenclature words, and what follows must start like an author
    string: '(' or an uppercase letter ('Gray, 1842', '(Kuhl, 1820)'). Hybrids, author
    particles, groupings and life-stage words ('complex', 'agg.', 'larva'), designations
    after 'sp.', and rank markers after an author are not split.
    """
    supplied = bool(authorship) and name.endswith(' ' + authorship)
    tokens = (name[:-len(authorship) - 1].rstrip() if supplied else name).split(' ')
    word = r"[^\W\d_]+(?:[-'][^\W\d_]+)*"
    if not re.fullmatch(word, tokens[0]) or not tokens[0][0].isupper():
        return None
    end = 1
    # A single capitalised word in parentheses straight after the genus is a subgenus, not an author.
    if len(tokens) > 1 and re.fullmatch(r'\(' + word + r'\)', tokens[1]) and tokens[1][1].isupper():
        end = 2
    while end < len(tokens) and (tokens[end] in RANK_MARKERS or tokens[end] in NAME_QUALIFIER_WORDS
                                 or (re.fullmatch(word, tokens[end]) and tokens[end].islower())):
        end += 1
    words, author = tokens[:end], authorship if supplied else ' '.join(tokens[end:])
    if supplied and end != len(tokens):
        return None
    if (any(token in AUTHOR_PARTICLES | NOT_EPITHETS for token in words[1:])
            or any(token.casefold() in HYBRID_MARKERS for token in tokens) or '×' in name
            # 'Aus sp. A' is an informal designation; 'Aus bus L. subsp. cus' has an infraspecific name after an author.
            or (author and (words[-1] in {'sp.', 'spp.', 'sp', 'spp'} or not (author[0] in '([' or author[0].isupper())
                            or any(token in RANK_MARKERS for token in author.split(' '))))):
        return None
    return words, author


def _qualified_name(name, qualifier, authorship=''):
    """Supplied name text carrying the supplied qualifier, or None when it cannot be placed.

    Text that already contains the qualifier, as words or attached at the end
    ('Rana cf. arvalis' + 'cf.', 'Pachyporidae?' + '?'), is kept as is. A multi-word
    qualifier names the part it qualifies ('aff. agrifolia var. oxyadenia'), so its
    first word goes where that part starts. A single 'cf.' or 'aff.' goes before the
    final epithet (and its rank marker) of a binomial or trinomial, as in
    'Tropidolaemus cf. subannulatus Gray, 1842'; other single words ('sp.', '?')
    follow the name before any authorship ('Iguana sp. ?'). When the name cannot be
    split from its authorship, or the part is not in the name, no text is constructed.
    """
    name = name.rstrip()
    if _words_at(name, qualifier) is not None or name.endswith(qualifier):
        return name
    first, _, rest = qualifier.partition(' ')
    if rest:
        position = _words_at(name, rest.strip())
        return None if position is None else f'{name[:position]}{first} {name[position:]}'
    split = _split_authorship(name, authorship.strip())
    if split is None:
        return None
    words, author = split
    # 'sp.', 'spp.' and 'indet.' say the name stops above species; beside a species epithet they contradict it.
    if qualifier.rstrip('.').casefold() in {'sp', 'spp', 'indet'} and any(
            word.islower() and word not in NAME_QUALIFIER_WORDS | RANK_MARKERS for word in words[1:]):
        return None
    if (qualifier.rstrip('.').casefold() in {'cf', 'aff'} and len(words) > 1 and words[-1].islower()
            and words[-1] not in NAME_QUALIFIER_WORDS | RANK_MARKERS):
        at = len(words) - 2 if len(words) > 2 and words[-2] in RANK_MARKERS else len(words) - 1
        words = [*words[:at], qualifier, *words[at:]]
    else:
        words = [*words, qualifier]
    return ' '.join([*words, *([author] if author else [])])


def _name_needs_review(value, authorship=''):
    """Recognise plain name forms, without parsing or removing source authorship.

    This only selects review questions; it does not validate nomenclature. Each
    hybrid component must pass independently, so an author after × is not hidden.
    """
    value = _without_authorship(value, authorship)
    word = r"[^\W\d_]+(?:[-'][^\W\d_]+)*"
    ranks = {'subsp.', 'ssp.', 'var.', 'subvar.', 'f.', 'fo.', 'forma', 'subf.',
             'nothosubsp.', 'nothovar.', 'cv.', 'convar.', 'agg.', 'sect.', 'ser.', 'subg.', 'subsect.'}
    for component in re.split(r'\s+(?:×|x)\s+', value):
        component = component.removeprefix('×')
        # A cultivar epithet is part of the name; author text outside it remains.
        component = re.sub(r" '[^']+'$", '', component)
        component = re.sub(rf'^({word}) \(({word})\)(?= [^\W\d_])', r'\1', component)
        parts = component.split(' ')
        if not parts or not re.fullmatch(word, parts[0]) or not parts[0][0].isupper():
            return True
        for part in parts[1:]:
            if part not in ranks and (not re.fullmatch(word, part) or not part.islower()):
                return True
        if any(part in {'cf', 'aff', 'sp', 'spp', 'nr', 'indet', 'ex', 'in', 'et', 'non', 'sensu'} for part in parts[1:]):
            return True
    return False


def _event_nodes(archive, core):
    """Event identities for parent resolution: (nodes, unsupported reason).

    Event cores: one node per source row. Occurrence cores: one node per supplied
    eventID, matching the reviewed by_id grouping. Archive join keys only map nodes
    back to emitted events; they never resolve parent values.
    """
    if PARENT not in core.terms:
        return None, None
    if DWC + "eventID" not in core.terms:
        return None, ("The core declares no dwc:eventID field, so parent values have no persistent identifiers to resolve against. "
                      "Archive row keys are never used as parents. The column stays in the originals.")
    id_column, parent_column = core.terms.index(DWC + "eventID"), core.terms.index(PARENT)
    known_ids = {row[id_column] for row in core.rows if row[id_column]}
    def parent_value(row):
        value = row[parent_column]
        return '' if missing_reference(value, known_ids) else value
    if core.row_type == DWC + "Event":
        return [{"event_id": row[id_column], "parents": {parent_value(row)}, "rows": [n + 1], "join_ids": [source_id]}
                for n, (row, source_id) in enumerate(zip(core.rows, core.ids))], None
    nodes, groups = [], {}
    for n, (row, source_id) in enumerate(zip(core.rows, core.ids)):
        event_id = row[id_column]
        if not event_id:
            if parent_value(row):
                nodes.append({"event_id": "", "parents": {parent_value(row)}, "rows": [n + 1], "join_ids": [source_id]})
            continue
        if event_id not in groups:
            groups[event_id] = {"event_id": event_id, "parents": set(), "rows": [], "join_ids": []}
            nodes.append(groups[event_id])
        groups[event_id]["parents"].add(parent_value(row)); groups[event_id]["rows"].append(n + 1); groups[event_id]["join_ids"].append(source_id)
    return nodes, None


def _depth_key(core, row):
    """A row's source depth values. Depth children are keyed by these, so a retained or invalid value never merges distinct depths."""
    return tuple(value for term, value in zip(core.terms, row) if term in {DWC + field for field in DEPTH_FIELDS})


def _depth_varies(core):
    """Some supplied eventID is shared by occurrence rows with different source depth values."""
    if DWC + 'eventID' not in core.terms:
        return False
    id_column = core.terms.index(DWC + 'eventID')
    depths = defaultdict(set)
    for row in core.rows:
        if row[id_column]:
            depths[row[id_column]].add(_depth_key(core, row))
    return any(len(values) > 1 for values in depths.values())


def _hierarchy(archive, core):
    nodes, unsupported = _event_nodes(archive, core)
    if nodes is None:
        return nodes, unsupported, None
    return nodes, unsupported, resolve_parents(nodes, core.ids if archive.has_meta else ())


def _scientific_hierarchy(archive, core, nodes, hierarchy):
    """Audit original assertions, independently of column filters and emitted values."""
    if nodes is None or not hierarchy or not hierarchy['links']:
        return None
    events, surveys, ambiguous, spatial_ambiguities = {}, defaultdict(list), [], defaultdict(list)
    by_join = {join: position for position, node in enumerate(nodes) for join in node['join_ids']}
    for position, node in enumerate(nodes):
        sources = [_source(core.terms, core.rows[row - 1]) for row in node['rows']]
        events[position] = {}
        for term in sorted({term for source in sources for term in source}):
            supplied = sorted({source.get(term, '') for source in sources})
            if len(supplied) == 1:
                events[position][term] = supplied[0]
            elif term not in {DWC + 'occurrenceID', PARENT} and term in {
                    DWC + field for field in ('eventDate', 'decimalLatitude', 'decimalLongitude',
                        'coordinateUncertaintyInMeters', 'geodeticDatum', 'footprintWKT',
                        'dataGeneralizations', 'informationWithheld', 'coordinatePrecision')}:
                ambiguous.append({'child': position, 'parent': hierarchy['links'].get(position),
                    'kind': 'event-source-ambiguity', 'status': 'incomparable',
                    'reason': 'Source rows grouped into this event disagree; no representative value is selected.',
                    'evidence': {'term': term, 'values': supplied}})
                if term != DWC + 'eventDate':
                    spatial_ambiguities[position].append({'term': term, 'values': supplied})
    for t, table in enumerate(archive.tables):
        if table.is_core or table.row_type not in HUMBOLDT_FAMILIES:
            continue
        for n, (row, source_id) in enumerate(zip(table.rows, table.ids)):
            if source_id in by_join:
                surveys[by_join[source_id]].append({'table': t, 'row': n + 1, 'values': {
                    term: value for term, value in _source(table.terms, row).items()
                    if term.startswith(('http://rs.tdwg.org/eco/terms/', 'http://rs.tdwg.org/eco/iri/'))}})
    audit = audit_hierarchy(nodes, hierarchy['links'], events, surveys)
    for check in audit['checks']:
        if check['kind'] == 'spatial':
            conflicts = {position: spatial_ambiguities[position] for position in
                         (check['child'], check['ancestor']) if position in spatial_ambiguities}
            if conflicts:
                check['status'] = 'incomparable'
                check['reason'] = 'Grouped source location values disagree; no spatial claim is made.'
                check['evidence'].pop('calculated', None)
                check['evidence']['group_ambiguities'] = [
                    {'event_index': position, 'fields': fields} for position, fields in sorted(conflicts.items())]
    audit['checks'].extend(ambiguous)
    audit['counts'] = dict(sorted(Counter(check['status'] for check in audit['checks']).items()))
    audit['has_findings'] = audit['has_findings'] or bool(ambiguous)
    audit['review_required'] = False
    # Store grouped source lineage once rather than repeating a large occurrence
    # group's rows in every descendant check. Checks reference these node indices.
    audit['events'] = [{'index': position, 'eventID': node['event_id'], 'sources': [
        {'source_table': core.name, 'source_row': row, **core.row_sources[row - 1]}
        for row in node['rows']]} for position, node in enumerate(nodes)]
    for check in audit['checks']:
        for role in ('child', 'parent', 'ancestor'):
            position = check.get(role)
            if position is not None:
                node = nodes[position]
                check[role + '_eventID'] = node['event_id']
    return {**audit, 'basis': 'Original source assertions, including fields retained without mapping.',
        'policy': 'Findings do not repair values or establish scientific equivalence. No scopes, completeness, '
                  'absences, effort or other properties are inherited or aggregated.',
        'reference': 'https://eco.tdwg.org/hierarchy/',
        'limitations': ['Temporal ancestor constraints are checked without inheriting event values.',
            'Spatial checks use only the nearest ancestor with supplied coordinates; polygons are not evaluated.',
            'Survey checks use the nearest ancestor with survey rows; literal and IRI values are not paired.',
            'Event dates and locations supplied by other extensions, or derived during conversion, are not audited.',
            'Supported checks cannot establish complete scientific consistency or authorize absence inference.']}


def _scientific_summary(audit):
    if audit is None:
        return None
    actionable = [check for check in audit['checks'] if check['status'] in
                  {'contradiction', 'conflict-signal', 'reporting-gap'} or
                  check['kind'] in {'survey-ambiguous', 'event-source-ambiguity'}]
    return {key: audit[key] for key in ('counts', 'has_findings', 'review_required', 'basis', 'policy', 'reference')} | {
        'checks': len(audit['checks']), 'finding_sample': actionable[:10]}


def _specimen_presence(table):
    """A conventional 'present' status for specimen records, as {'default', 'title', 'reason'}, or None.

    Only when no row supplies occurrenceStatus, every row declares a specimen or
    sample basisOfRecord, and no cell uses absence wording. Callers also exclude
    zero counts. GBIF interprets such records as present.
    """
    status = table.terms.index(DWC + 'occurrenceStatus') if DWC + 'occurrenceStatus' in table.terms else None
    basis = table.terms.index(DWC + 'basisOfRecord') if DWC + 'basisOfRecord' in table.terms else None
    if (not table.rows or basis is None or any(row[status] for row in table.rows if status is not None)
            or any(row[basis].strip() not in SPECIMEN_BASES for row in table.rows)
            or any(ABSENCE_WORDING.search(value) for row in table.rows for value in row if value)):
        return None
    bases = ', '.join(sorted({row[basis].strip() for row in table.rows}))
    return {'default': 'present', 'title': f'{table.name}: specimen records are recorded as present', 'reason': (
        f'Every row of {table.name} is a specimen or sample record ({bases}) and none says whether the organism was present. '
        'No count is zero and no row uses absence wording, so occurrenceStatus is filled with present, as GBIF '
        'would interpret these records. Change this to absent if the records report organisms that were not found.')}


def _issue(id, title, reason, options, **extra):
    """A review question. kind and per-option assertion flags are set by apply_policy."""
    return {"id": id, "title": title, "reason": reason, "options": options, **extra}


def _streamline_plan(archive, core, plan, warnings):
    """Separate decisions about meaning from deterministic copies and retention.

    Automatic choices remain overrideable, validated and included in the plan hash.
    A lone confirmation (notably loose-file layout) is still a real user decision.
    """
    automatic, required = [], []
    columns = {column['id']: column for column in plan['columns']}
    event_ids = core.terms.index(DWC + 'eventID') if DWC + 'eventID' in core.terms else None
    category = core.terms.index(DWC + 'eventCategory') if DWC + 'eventCategory' in core.terms else None
    categories = {source_id: row[category] for source_id, row in zip(core.ids, core.rows)} if category is not None else {}
    category_choice = any(issue['id'] == 'event-category' for issue in plan['issues'])
    if core.row_type == DWC + 'Event':
        for issue in plan['issues']:
            if not issue['id'].startswith('table:'):
                continue
            table = archive.tables[issue['table']]
            if table.row_type in HUMBOLDT_FAMILIES and any(categories.get(source_id) not in {None, '', 'survey'} for source_id in table.ids):
                reason = ('A supplied non-survey event category cannot be overwritten. The Humboldt table is retained in originals; '
                          'correct the source classification before converting these survey rows.')
                issue.update(options=[PRESERVE], reason=reason)
                plan['tables'][issue['table']]['conversion_unavailable'] = reason
    retained_tables = {issue['table'] for issue in plan['issues'] if issue['id'].startswith('table:')
                       and {option['value'] for option in issue['options']} == {'preserve'}}
    for column in plan['columns']:
        if column['table'] in retained_tables:
            column.update(default='preserve', options=[PRESERVE], review=False)
    for issue in plan['issues']:
        default = None
        reason = None
        if issue.get('table') in retained_tables and not issue['id'].startswith('table:'):
            if not issue['id'].startswith(('column:', 'row:', 'row-group:')):
                # This table cannot be converted, so dependent interpretations
                # cannot be applied and offer no actionable user choice.
                continue
            issue = {**issue, 'options': [PRESERVE]}
            default = 'preserve'
            reason = 'The whole extension is retained in originals because its subject cannot be represented by this converter.'
        elif {option['value'] for option in issue['options']} == {'preserve'}:
            default = 'preserve'
            reason = issue['reason'] if not issue['id'].startswith('table:') else (
                plan['tables'][issue['table']].get('conversion_unavailable') or
                archive.tables[issue['table']].unplaced_reason or
                'This extension has no supported subject or row-grain mapping for the declared core. It is retained in originals without creating target records.')
        elif issue['id'] == 'event-grain' and (event_ids is None or any(not row[event_ids] or
                missing_reference(row[event_ids], ()) for row in core.rows)):
            default = 'per_row'
            reason = 'Not every occurrence supplies a usable eventID. Separate context events keep each row; no event identities are merged.'
            issue = {**issue, 'options': [option for option in issue['options'] if option['value'] == 'per_row']}
            supplied = [row[event_ids] for row in core.rows if row[event_ids] and
                        not missing_reference(row[event_ids], ())] if event_ids is not None else []
            if len(set(supplied)) != len(supplied):
                # Splitting a repeated persistent identity is an interpretation,
                # even when missing IDs make complete grouping unavailable.
                required.append({**issue, 'reason': reason + ' Some supplied IDs repeat. Confirm separate row contexts, each inside one event for its repeated ID, or retain eventID in originals using the automatic mappings below.'})
                continue
        elif issue['id'] == 'event-grain' and len({row[event_ids] for row in core.rows}) == len(core.rows):
            default = 'by_id'
            reason = 'Every occurrence supplies a distinct eventID. Each row creates its own context event; no records are merged.'
        elif issue.get('convention'):
            # A default GBIF infers anyway: applied visibly and changeable, never asked.
            default, reason = issue['convention']['default'], issue['convention']['reason']
        elif issue['id'].startswith('material:') and issue.get('strong_specimen_signal'):
            default = 'per_row'
            reason = ('Every source row explicitly declares PreservedSpecimen and has a distinct, complete '
                      'institutionCode, collectionCode, catalogNumber triple. One material entity is created '
                      'for each source row; no persistent material identifier is inferred.')
        elif issue['id'] == AGENT_NAMES_ID:
            default = 'shared'
            # Other choices (a column kept in the originals, a name kept as text) can lower these counts;
            # the conversion report restates the notice with what was linked.
            reason = (f"Links up to {issue['count']:,} {'mention' if issue['count'] == 1 else 'mentions'} of "
                      f"{issue['names']:,} {'name' if issue['names'] == 1 else 'names'} without identifiers to one agent per "
                      "exact name. Choose 'Keep all names without identifiers as text only' if equal names may stand "
                      "for different people or organizations. Names with identifiers are linked by their identifier.")
        elif issue['id'].startswith('agent-share:'):
            default = 'shared'
            reason = (f"Linked the {issue['count']:,} mentions of {issue['source_value'][:100]!r} to one agent. Choose "
                      "'Keep this name as text only' if these are different people or organizations.")
        elif issue['id'].startswith('table:'):
            table = archive.tables[issue['table']]
            family = SUPPORTED_EXTENSIONS.get(table.row_type)
            # These row types declare the subject or relationship directly. Keep
            # each source row; no merging, depicted subject or material is inferred.
            if not table.unplaced_reason:
                if family == 'occurrence' and core.row_type == DWC + 'Event':
                    default = 'occurrence'
                elif family == 'identification' and core.row_type == DWC + 'Occurrence':
                    default = 'identification'
                elif family == 'relationship':
                    default = 'resource-relationship'
                elif family == 'assertion':
                    # A failed occurrence link does not establish that a fact
                    # describes the surrounding event. Ask for that choice.
                    if not any(option['value'] in {'occurrence-assertion', 'declared-assertions'}
                               for option in issue.get('unavailable_options', [])):
                        default = issue['options'][0]['value']
                elif family in {'identifier', 'reference'}:
                    default = family
                elif family == 'humboldt' and core.row_type == DWC + 'Event':
                    default = 'humboldt-survey'
                if default:
                    reason = ROLE_DESCRIPTIONS.get(default, 'Each row of {table} keeps its link to {core}.').format(table=table.name, core=core.name) + (
                        ' This follows from meta.xml.' if archive.has_meta else ' This follows from the file names and shared identifiers.')
        elif issue['id'].startswith('occurrence-events:') and 'patch' in {option['value'] for option in issue['options']}:
            # Requirements move this back to a question when any event's details disagree.
            default = 'patch'
            reason = (f"The occurrences of each event agree on these details, so they are copied onto the linked event in {core.name}. "
                      "No values are combined or invented.")
        elif issue['id'].startswith('hum-category:') and core.row_type == DWC + 'Event':
            table = archive.tables[issue['table']]
            if all(categories.get(source_id) == 'survey' for source_id in table.ids):
                default = 'require'
                reason = 'Every linked source event is already explicitly classified as a survey.'
            elif category_choice and all(categories.get(source_id, '') in {'', 'survey'} for source_id in table.ids):
                default = 'require'
                reason = 'Categories come from the source or your missing-event-category choice. This step requires survey categories and never fills or changes them.'
                issue = {**issue, 'depends_on_event_category': True}
        if default is None:
            required.append(issue)
            continue
        # The question's own explanation is kept for when a failing requirement turns this back into a question.
        automatic.append({**issue, 'default': default, 'reason': reason, 'question_reason': issue['reason']})
        if issue['id'] in columns:
            columns[issue['id']]['review'] = False
        # Unmapped columns are summarised from the column list, so they need no separate notice.
        if ((default == 'preserve' and not issue['id'].startswith('column:')) or (issue['id'] == 'event-grain' and default == 'per_row')
                or issue.get('convention') or issue['id'] == AGENT_NAMES_ID):
            notice = {**issue, 'reason': reason, **({'title': issue['convention']['title']} if issue.get('convention') else {})}
            warnings.append({key: value for key, value in notice.items() if key != 'options'})
    plan['issues'] = required
    plan['automatic_choices'] = automatic
    plan['warnings'] = warnings


def build_plan(archive):
    core = next(table for table in archive.tables if table.is_core)
    if core.row_type == DWC + 'Taxon':
        from api.dwca_taxon import build_taxon_plan
        plan = build_taxon_plan(archive)
        if getattr(archive, 'tidy', None):
            for column in plan['columns']:
                column.update(column_note(archive, column['table'], column['column']) or {})
            plan['tidy'] = {'version': archive.tidy['version'], 'sha256': archive.tidy['sha256']}
            plan['id'] = hashlib.sha256(json.dumps({key: value for key, value in plan.items() if key != 'id'}, sort_keys=True).encode()).hexdigest()
        return plan
    material_context = core.row_type == DWC + 'Occurrence' and any(
        term in core.terms and any(row[core.terms.index(term)] for row in core.rows)
        for term in (DWC + 'materialSampleID', DWC + 'materialEntityID'))
    issues, columns, profiles, warnings, row_issues = [], [], [], dropped_extension_warnings(archive), []
    nodes, hierarchy_unsupported, hierarchy = _hierarchy(archive, core)
    scientific = _scientific_hierarchy(archive, core, nodes, hierarchy)
    if not archive.has_meta:
        issues.append(_issue("loose-links", "Check how your files fit together",
            "There is no meta.xml, so file roles were recognised from the file names, and rows are linked by their shared identifiers (never by row order). Confirm that the table above is right.",
            [{"value": "confirm", "label": "Yes, these file roles and links are right"}]))
    if core.row_type == DWC + "Occurrence":
        supplied_events = [row[core.terms.index(DWC + 'eventID')] for row in core.rows
                           if row[core.terms.index(DWC + 'eventID')] and
                           not missing_reference(row[core.terms.index(DWC + 'eventID')], ())] if DWC + 'eventID' in core.terms else []
        depth_split = _depth_varies(core)
        repeated_events = len(set(supplied_events)) != len(supplied_events)
        issues.append(_issue("event-grain", "Should occurrences that share an eventID share one event?",
            "In a Data Package every occurrence belongs to an event, which holds where and when it was recorded. Occurrences with the same eventID can share one event, "
            "but only when their event details such as date and place agree. Otherwise each occurrence row keeps its own event"
            + (", and rows that share an eventID sit inside one event that keeps that eventID." if repeated_events else ".")
            + (" Some occurrences that share an eventID were sampled at different depths, for example by one cast that sampled several depths. "
               "Those can share one event for the eventID, with a child event inside it for each depth." if depth_split else ""),
            [{"value": "per_row", "label": "Give each occurrence row its own event"
              + (", inside one event for each shared eventID" if repeated_events else "")},
             {"value": "by_id", "label": "Combine occurrences with the same eventID into one event"},
             *([{"value": DEPTH_SPLIT, "label": "Combine occurrences with the same eventID into one event, with a child event for each depth"}]
               if depth_split else [])],
            # Splitting a repeated persistent identity asserts that the rows are different events,
            # and so does splitting one event into a sub-event per depth.
            assertion_values=[*(['per_row'] if repeated_events else []),
                              *([DEPTH_SPLIT] if depth_split else [])]))
    for t, table in enumerate(archive.tables):
        family = SUPPORTED_EXTENSIONS.get(table.row_type)
        sources = [_source(table.terms, row) for row in table.rows] if family in {'humboldt', 'eol-media', 'eol-reference', 'bmde', 'nbn'} else []
        humboldt_blocked = Counter(term for source in sources for term in blocked_fields(source)) if family == 'humboldt' else None
        if not table.is_core:
            options = [PRESERVE]
            if family == "occurrence" and core.row_type == DWC + "Event":
                options.insert(0, {"value": "occurrence", "label": "Occurrences, each linked to its event"})
            elif family == "identification":
                options.insert(0, {"value": "identification", "label": "Identification records of linked occurrences"}) if core.row_type == DWC + "Occurrence" else None
            elif family == "assertion":
                explicit_occurrences = DWC + 'occurrenceID' in table.terms and any(row[table.terms.index(DWC + 'occurrenceID')] for row in table.rows)
                if core.row_type == DWC + 'Event' and explicit_occurrences:
                    options.insert(0, {'value': 'event-assertion', 'label': 'Assertions about linked events'})
                    options.insert(0, {'value': 'declared-assertions', 'label': 'Use supplied occurrenceID; otherwise the linked core event'})
                else:
                    options.insert(0, {"value": "event-assertion", "label": "Assertions about linked events"})
                if core.row_type == DWC + "Occurrence":
                    options.insert(0, {"value": "occurrence-assertion", "label": "Assertions about linked occurrences"})
            elif family == "relationship":
                options.insert(0, {"value": "resource-relationship", "label": "Generic resource relationships, keeping supplied identifiers"})
            elif family == "molecular":
                options.insert(0, {"value": "molecular", "label": "Sequence, protocol and analysis records in linked event context"})
            elif family in {'media', 'eol-media'}:
                options.insert(0, {'value': 'media-unlinked', 'label': 'Media resources without an inferred subject link'})
                options.insert(0, {'value': 'media-event', 'label': 'Media depicts the linked event context'})
                if core.row_type == DWC + 'Occurrence':
                    options.insert(0, {'value': 'media-occurrence', 'label': 'Media depicts the linked occurrence'})
            elif family in {'identifier', 'reference', 'eol-reference'}:
                options.insert(0, {'value': family, 'label': ('Alternative identifiers of the linked core record' if family == 'identifier' else 'Bibliographic references to the linked core record')})
            elif family == 'humboldt':
                if core.row_type == DWC + 'Event':
                    options[:0] = [{'value': 'humboldt-survey', 'label': 'Each source row describes a separate survey of its event'},
                                   {'value': 'humboldt-merge', 'label': 'One survey per event; require identical source rows'}]
                elif DWC + 'eventID' in core.terms and all(
                        row[core.terms.index(DWC + 'eventID')] and
                        not missing_reference(row[core.terms.index(DWC + 'eventID')], ()) for row in core.rows):
                    options.insert(0, {'value': 'humboldt-grouped', 'label': 'Survey of shared eventID; require complete identical coverage'})
            elif family == 'germplasm-accession' and material_context:
                options.insert(0, {'value': 'germplasm-accession', 'label': 'Accession identifiers and passport statements of reviewed core material'})
            elif family == 'germplasm-score':
                options.insert(0, {'value': 'germplasm-score-event', 'label': 'Scores describe the linked event context'})
                if core.row_type == DWC + 'Occurrence':
                    options.insert(0, {'value': 'germplasm-score-occurrence', 'label': 'Scores describe the linked occurrence'})
                if material_context:
                    options.insert(0, {'value': 'germplasm-score-material', 'label': 'Scores describe reviewed core material; require matching accession identifiers'})
            elif family == 'germplasm-trait':
                options.insert(0, {'value': 'germplasm-trait', 'label': 'Trait descriptors are dataset-level protocol descriptions'})
            elif family == 'germplasm-trial' and core.row_type == DWC + 'Event':
                options.insert(0, {'value': 'germplasm-trial', 'label': 'Trial describes the linked core event; reject conflicting values'})
            elif family in {'bmde', 'nbn'}:
                options.insert(0, {'value': family + '-context', 'label': ('BMDE measurements of the occurrence and reviewed event context' if family == 'bmde' and core.row_type == DWC + 'Occurrence' else 'Reviewed values of the linked event context')})
            media_signals = [term for term in table.terms if term in MEDIA_SUBJECT_TERMS and any(row[table.terms.index(term)] for row in table.rows)] if family == 'media' else []
            issues.append(_issue(f"table:{t}", f"{table.name}: what do these rows describe?",
                table.unplaced_reason or ('Being attached to a record does not say what a picture or recording shows. Choose whether each media file shows the linked occurrence or its event, keep the media without a link, or keep the file only in your originals.' if family == 'media' else f"Choose what the rows of {table.name} describe and how they link to {core.name}. Anything not converted stays in your original files."), options,
                **({'subject_review_signals': media_signals} if family == 'media' else {}),
                table=t, row_type=table.row_type, rows=len(table.rows)))
        own = "event" if table.row_type == DWC + "Event" else "occurrence"
        specimen_terms = (DWC + 'institutionCode', DWC + 'collectionCode', DWC + 'catalogNumber')
        specimen_columns = [table.terms.index(term) for term in specimen_terms] if all(term in table.terms for term in specimen_terms) else []
        basis_column = table.terms.index(DWC + 'basisOfRecord') if DWC + 'basisOfRecord' in table.terms else None
        specimen_keys = [tuple(row[c].strip() for c in specimen_columns) for row in table.rows] if specimen_columns else []
        specimen_context = bool(table.is_core and table.row_type == DWC + 'Occurrence' and table.rows
            and basis_column is not None and specimen_columns
            and all(row[basis_column].strip() == 'PreservedSpecimen' for row in table.rows)
            and any(any(key) for key in specimen_keys))
        strong_specimen = bool(specimen_context
            and all(all(value and not missing_reference(value, ()) for value in key) for key in specimen_keys)
            and len(set(specimen_keys)) == len(specimen_keys))
        has_material = (table.is_core or family == 'occurrence') and own == 'occurrence' and any(
            term in table.terms and any(row[table.terms.index(term)] for row in table.rows)
            for term in (DWC + 'materialSampleID', DWC + 'materialEntityID')) or specimen_context
        if has_material:
            issues.append(_issue(f'material:{t}', f'{table.name}: do these identify physical specimens or samples?',
                ('Every row declares PreservedSpecimen and has a distinct institutionCode, collectionCode, catalogNumber triple. '
                 if strong_specimen else 'The source declares preserved specimens with catalog fields, but some catalog identities repeat or are incomplete. '
                 if specimen_context else 'Some rows have material sample identifiers. ') +
                'If these identify physical things, such as a specimen, tissue or soil sample, they can become material records: '
                'one per row, or one per identifier when all of its rows agree. Otherwise keep the identifiers only in your original files.',
                [PRESERVE, {'value': 'per_row', 'label': 'Yes: one material record per row'},
                 {'value': 'by_id', 'label': 'Yes: one material record per identifier'}],
                **({'strong_specimen_signal': True, 'table': t} if strong_specimen else {})))
        target_tables = [own] + (["event", "identification"] if own == "occurrence" else []) + (["material"] if has_material else []) if table.is_core or family == "occurrence" else {
            "identification": ["identification"], "assertion": ["occurrence-assertion"],
            "relationship": ["resource-relationship"], "molecular": ["molecular-protocol"],
            'media': ['media', 'usage-policy', 'provenance'],
            'identifier': ['occurrence-identifier'], 'reference': ['bibliographic-resource'],
            'humboldt': ['survey'],
        }.get(family, [])
        profile = {"name": table.name, "row_type": table.row_type, "core": table.is_core,
                   "rows": len(table.rows), "columns": [], "unique_join_ids": len(set(table.ids)), 'join_basis': table.join_basis}
        for c, term in enumerate(table.terms):
            values = [row[c] for row in table.rows if row[c] != ""]
            options = _candidates(term, target_tables)
            media_reason = None
            if family == 'media':
                options, media_reason = media_targets(term, values)
            if family in {'eol-media', 'eol-reference'}:
                options, media_reason = eol_targets(table.row_type, term, values)
                if family == 'eol-reference' and options and any(source.get('http://eol.org/schema/reference/full_reference') for source in sources) and term != 'http://eol.org/schema/reference/full_reference':
                    media_reason = (media_reason or '') + ' Full citations are supplied; EOL says structured fields are ignored in that case. Confirm this field should also be copied, or preserve it.'
            if family == 'humboldt':
                options, media_reason = humboldt_targets(term, sources, humboldt_blocked)
                if term in DIRECT or term in IRI_DIRECT:
                    # Validation can withhold incompatible cells without guessing
                    # their meaning. A strict agent IRI keeps its identifier role.
                    if media_reason:
                        warnings.append({'id': _column_id(t, c), 'title': term.rsplit('/', 1)[-1],
                            'reason': media_reason, 'table': t, 'nonempty': len(values)})
                    media_reason = None
            if family in GERMPLASM_FAMILIES.values():
                options, media_reason = germplasm_targets(table.row_type, term, values)
                if (table.row_type, term) in GERMPLASM_DERIVED_TERMS:
                    options = ['derive']
            if family in LEGACY_FAMILIES.values():
                options, media_reason = legacy_targets(table.row_type, term, values)
                if options and (table.row_type, term) in LEGACY_DERIVED_TERMS:
                    options = ['derive']
                if family == 'bmde' and core.row_type == DWC + 'Event' and any(term in group for group in GROUPS):
                    options = []; media_reason = 'BMDE measurements require an occurrence subject. Event-core attachment alone cannot establish one.'
            if family in {'identifier', 'reference'}:
                options = reference_targets(table.row_type, term)
                if core.row_type == DWC + 'Event':
                    options = [target.replace('occurrence-', 'event-', 1) for target in options]
                media_reason = NON_EXACT_TARGETS.get((table.row_type, term))
                if (table.row_type, term) in {(IDENTIFIER_ROW_TYPE, DC + 'identifier'), (REFERENCE_ROW_TYPE, DC + 'identifier')}:
                    media_reason = None
            # Explicit, reviewed aliases between standards. No basename matching of IRIs.
            aliases = {
                DWC + "resourceID": "resource-relationship.subjectResourceID",
                DWC + "relationshipOfResource": "resource-relationship.relationshipType",
                DWC + "relationshipAccordingTo": "resource-relationship.relationshipAccordingTo",
                DWC + "relationshipEstablishedDate": "resource-relationship.relationshipEstablishedDate",
            } if family == "relationship" else {}
            assertions = {DWC + source: "occurrence-assertion." + target for source, target in {
                "measurementID": "assertionID", "measurementType": "verbatimAssertionType",
                "measurementValue": "assertionValue", "measurementUnit": "assertionUnit",
                "measurementAccuracy": "assertionError", "measurementDeterminedDate": "assertionMadeDate",
                "measurementDeterminedBy": "assertionBy", "measurementMethod": "assertionProtocols",
                "measurementRemarks": "assertionRemarks",
            }.items()} | {OBIS + source: "occurrence-assertion." + target for source, target in OBIS_IRI_ALIASES.items()
                          } if family == "assertion" else {}
            if term in aliases or term in assertions:
                options = [aliases.get(term) or assertions[term]]
            if family == "molecular" and term == "http://rs.gbif.org/terms/dna_sequence":
                options = ["nucleotide-sequence.sequence"]
            if has_material and term == DWC + 'materialSampleID':
                options = ['material.materialEntityID']
            if term == DWC + 'recordedByID' and own == 'occurrence' and (table.is_core or family == 'occurrence'):
                options = ['occurrence.recordedByID']
            parent_blocked = parent_default = None
            if term == PARENT and (table.is_core or family == 'occurrence'):
                # Parent links are resolved from persistent eventIDs of source Event identities, never copied as keys.
                if not table.is_core:
                    parent_blocked = ('parentEventID in an Occurrence extension describes an event, not this occurrence row. '
                                      'Parent links are resolved only from core Event identities; values stay in the originals.')
                elif hierarchy_unsupported or not hierarchy['links']:
                    parent_blocked = (hierarchy_unsupported or (describe_problems(hierarchy) if hierarchy['problems'] else 'No supplied parentEventID identifies a source event.')
                                      + ' No supplied links can be resolved; keep the column in the originals.')
                elif own == 'occurrence' and any(not row[core.terms.index(DWC + 'eventID')] or
                        missing_reference(row[core.terms.index(DWC + 'eventID')], ()) for row in core.rows):
                    parent_blocked = ('Not every source occurrence supplies an eventID. Parent links require established event identities; '
                                      'values remain in the originals without inferring missing events.')
                options, media_reason = ([], parent_blocked) if parent_blocked else ([PARENT_LINK], None)
                if not parent_blocked and hierarchy['problems']:
                    media_reason = (describe_problems(hierarchy)
                                    + ' Only independently resolvable links are created; unresolved values stay in the originals and report.')
                if not parent_blocked and own == 'occurrence' and len({row[core.terms.index(DWC + 'eventID')] for row in core.rows}) != len(core.rows):
                    parent_default = 'preserve'
                    media_reason = (media_reason + ' ' if media_reason else '') + ('Occurrence rows do not establish events by themselves. Parent links are available only when events are '
                                    'combined by supplied eventID, and each parent eventID is itself such a combined event. '
                                    'No parent event is created or inferred.')
            chosen = parent_default or (options[0] if options else "preserve")
            join_only = (table.loose and not table.is_core and term == DWC + ('eventID' if core.row_type == DWC + 'Event' else 'occurrenceID') and bool(table.ids)) or (family == 'assertion' and term == DWC + 'occurrenceID')
            if join_only:
                chosen = 'join'
            incompatible = {} if family == 'humboldt' else {
                target: sum(bool(_copy_rejection(table.row_type, target, value)) for value in values
                            if not (target.endswith(ASSERTION_IRI_FIELDS) and missing_reference(value)))
                for target in options if '.' in target}
            incompatible = {target: count for target, count in incompatible.items() if count}
            typed_reason = (f"Some source values fail the target's type, bounds, or semantic constraints: "
                            + ', '.join(f'{target} ({count} values)' for target, count in incompatible.items())
                            + '. Mapping copies compatible cells only; invalid values stay in originals and the report. Preserve the column if its meaning needs clarification.') if incompatible else None
            # Prefer an exact field on the declared subject over duplicate annotations
            # on identification/material fields. A different subject still needs review.
            exact_subject_default = False
            if (table.is_core or family == 'occurrence') and len(options) > 1:
                own_options = [target for target in options if target.startswith(own + '.')]
                if len(own_options) == 1:
                    chosen = own_options[0]
                    exact_subject_default = True
            name_ambiguity = bool(options) and term == DWC + 'scientificName' and any(
                _name_needs_review(source.get(term, ''), source.get(DWC + 'scientificNameAuthorship', ''))
                for row in table.rows for source in [_source(table.terms, row)] if source.get(term))
            date_ambiguity = bool(options) and term == DWC + 'eventDate' and any(
                re.fullmatch(r'[+-]?[0-9]{4}\.[0-9]+', value.strip()) or
                value.strip().lower() in {'na', 'n/a', 'null', 'none', 'unknown', 'not recorded'} for value in values)
            date_reason = ('Some event dates are float-shaped years or unknown-value tokens. The original text is copied without repair or interpreting these tokens as empty. Their meaning remains unverified.') if date_ambiguity else None
            subject_move = chosen.startswith('material.') and term not in {DWC + 'materialSampleID', DWC + 'materialEntityID'} and not (
                strong_specimen and term in {*specimen_terms, DWC + 'preparations'})
            review = bool(not join_only and values and (not options or (len(options) > 1 and not exact_subject_default) or media_reason or subject_move or name_ambiguity or (own == 'occurrence' and term == DWC + 'typeStatus') or chosen.startswith('molecular-protocol.env_') or (has_material and term == DWC + 'recordedBy')))
            item = {"id": _column_id(t, c), "table": t, "column": c, "term": term,
                    "default": chosen, "review": review, "options": ([{'value': 'join', 'label': 'Used to join source rows to the core'}] if join_only else [_choice(value) for value in options] + ([] if chosen in {'occurrence.occurrenceStatus', 'event.eventCategory'} else [PRESERVE])),
                    "nonempty": len(values), "distinct": len(set(values)), "samples": [value[:500] for value in list(dict.fromkeys(values))[:3]],
                    **({'incompatible_values': incompatible} if incompatible else {}),
                    **({'parent_link_unavailable': parent_blocked} if parent_blocked else {})}
            if parent_default:
                item['options'] = [PRESERVE] + [option for option in item['options'] if option['value'] != 'preserve']
            if not options and not join_only:
                # Distinguish a term the Data Package has no field for from one this converter does not map yet.
                item['unmapped'] = 'unsupported' if term in SCHEMA_TERMS else 'no-target'
            if term == NAME and ((own == 'occurrence' and (table.is_core or family == 'occurrence')) or family == 'identification'):
                # The supplied name text is always copied to verbatimIdentification, whatever is chosen here.
                item['verbatim_copy'] = ('identification' if family == 'identification' else 'occurrence') + '.verbatimIdentification'
                partial = VERBATIM_NAME in table.terms
                if partial:
                    # Rows whose own verbatimIdentification is converted keep it; the copy fills only empty ones.
                    item['verbatim_source'] = _column_id(t, table.terms.index(VERBATIM_NAME))
                label = ('Leave scientificName empty; the name text fills verbatimIdentification wherever that would otherwise be empty' if partial
                         else 'Leave scientificName empty; the name text is kept in verbatimIdentification')
                item['options'] = [{**option, 'label': label} if option['value'] == 'preserve' else option for option in item['options']]
            qualifier_copy = (term == QUALIFIER and NAME in table.terms and not options and
                              ((own == 'occurrence' and (table.is_core or family == 'occurrence')) or family == 'identification'))
            if qualifier_copy:
                # The qualifier follows the name text wherever verbatimIdentification is filled from scientificName.
                item.pop('unmapped', None)
                item.update(verbatim_copy=('identification' if family == 'identification' else 'occurrence') + '.verbatimIdentification',
                            verbatim_role='qualifier')
                if VERBATIM_NAME in table.terms:
                    item['verbatim_source'] = _column_id(t, table.terms.index(VERBATIM_NAME))
            item.update(column_note(archive, t, c) or {})
            columns.append(item); profile["columns"].append({key: item[key] for key in ("term", "nonempty", "distinct", "samples")})
            if typed_reason or date_reason:
                warnings.append({'id': item['id'], 'title': term.rsplit('/', 1)[-1],
                    'reason': ' '.join(filter(None, (typed_reason, date_reason))), 'table': t,
                    'nonempty': len(values), 'samples': item['samples']})
            if review:
                issues.append(_issue(item["id"], term.rsplit("/", 1)[-1],
                    kind='name-semantics' if name_ambiguity and not media_reason else 'column-mapping', reason=media_reason or (
                        "Some names look like they include an author, a qualifier such as 'cf.', or another unusual form. In a Darwin Core Data Package, scientificName holds only the name, without its author. "
                        + ("The full text fills verbatimIdentification wherever that would otherwise be empty, and your original files keep everything. "
                           if VERBATIM_NAME in table.terms else "The full text is always kept in verbatimIdentification. ")
                        + "Copy the names into scientificName as they are, or leave scientificName empty."
                        if name_ambiguity else ("Darwin Core Data Packages have no identificationQualifier field, and scientificName excludes qualifiers. "
                        "Each qualifier is added after the name text in verbatimIdentification wherever that is filled from scientificName, "
                        "so the uncertainty stays visible; your original files keep the column too.") if qualifier_copy
                        else "This column could describe more than one thing, for example the occurrence or its identification. Choose where it belongs, or keep it only in your original files." if options
                        else "Darwin Core Data Packages have no field for this term, so the values stay in your original files." if term not in SCHEMA_TERMS
                        else "This converter does not map this term yet, so the values stay in your original files."),
                    options=item["options"], table=t, nonempty=len(values), samples=[value[:250] for value in item["samples"]]))
            # Labels the tidy-up settled are no longer asked about; one it offers as a suggestion is not asked twice.
            suggested = pending_suggestions(archive, t, c)
            if table.is_core and own == 'occurrence' and term == DWC + 'countryCode' and chosen == 'event.countryCode':
                for source_value, count in sorted(Counter(values).items()):
                    if not semantic_target_rejection('event.countryCode', source_value) or source_value in suggested:
                        continue
                    issues.append(_issue(_reviewed_value_id('country-label', t, c, source_value),
                        f'{table.name}: where does {source_value[:100]!r} belong?',
                        f'{count} source rows put {source_value[:200]!r} in countryCode, but it is not an ISO country code. '
                        'Choose whether this exact label names a country, a water body, or belongs only in the originals. '
                        'The selected target receives the source text unchanged; no ISO code is inferred.',
                        [PRESERVE, {'value': 'event.country', 'label': 'Country or territory name'},
                         {'value': 'event.waterBody', 'label': 'Water body name'}],
                        # Reading the supplier's own label into the field it names is an interpretation, not a new fact.
                        kind='column-mapping', table=t, source_column=c, source_value=source_value, count=count))
            if table.is_core and own == 'occurrence' and term == DWC + 'eventRemarks' and chosen == 'event.eventRemarks':
                event_ids = [row[table.terms.index(DWC + 'eventID')] for row in table.rows
                             if row[table.terms.index(DWC + 'eventID')]] if DWC + 'eventID' in table.terms else []
                if len(event_ids) == len(set(event_ids)):
                    for source_value, count in sorted(Counter(values).items()):
                        if not is_age_like_remark(source_value) or source_value in suggested:
                            continue
                        issues.append(_issue(_reviewed_value_id('age-remark', t, c, source_value),
                            f'{table.name}: does {source_value[:100]!r} describe the organism?',
                            f'{count} source rows use {source_value[:200]!r} as eventRemarks. '
                            'Choose the subject for this exact text. Life stage keeps the text verbatim, including abbreviations; '
                            'mixed count, sex, or egg notes may fit occurrenceRemarks better. No value is interpreted or normalized.',
                            [{'value': 'event.eventRemarks', 'label': 'Keep as an event remark'},
                             {'value': 'occurrence.lifeStage', 'label': 'Organism life stage (verbatim)'},
                             {'value': 'occurrence.occurrenceRemarks', 'label': 'Occurrence remark (verbatim)'},
                             PRESERVE],
                            # Choosing the subject of the supplier's own words interprets them; nothing new is asserted.
                            kind='column-mapping', table=t, source_column=c, source_value=source_value, count=count))
        profiles.append(profile)
        if family == 'occurrence' and not table.is_core and core.row_type == DWC + 'Event':
            event_columns = [column for column in columns if column['table'] == t and column['nonempty'] and column['default'] != 'join'
                             and any(option['value'].startswith('event.') for option in column['options'])]
            # Only columns that go to the event by default are described; others are listed if chosen later.
            shown = [column['term'].rsplit('/', 1)[-1] for column in event_columns
                     if column['term'] != DWC + 'eventID' and column['default'].startswith('event.')]
            if shown:
                issues.append(_issue(f'occurrence-events:{t}', f'{table.name}: where and when details on occurrence rows',
                    f"{table.name} has event details on its rows ({', '.join(shown[:6])}{', …' if len(shown) > 6 else ''}). In a Data Package these belong to an event. "
                    f"They can be copied onto the linked event in {core.name} when all occurrences of that event agree, or each occurrence row can become its own event "
                    f"inside the linked event.",
                    [{'value': 'patch', 'label': f'Copy them onto the linked event in {core.name}'},
                     {'value': 'per-row', 'label': 'Make each occurrence row its own event, inside the linked event'},
                     {**PRESERVE, 'label': 'Keep these details only in your original files'}],
                    table=t, assertion_values=['per-row']))
        if table.is_core or family == 'occurrence':
            pair = [DWC + 'decimalLatitude', DWC + 'decimalLongitude']
            fields = [TABLE_SPECS['event'].field_descriptors[name] for name in ('decimalLatitude', 'decimalLongitude')]
            partial, invalid = 0, 0
            for row in table.rows:
                source = _source(table.terms, row)
                supplied = [source.get(term, '') for term in pair]
                copied = [bool(value) and valid_value(field, value) for field, value in zip(fields, supplied)]
                if copied[0] != copied[1]:
                    partial += 1
                    invalid += any(value and not valid_value(field, value) for field, value in zip(fields, supplied))
            if partial:
                warnings.append({'id': f'coordinate-pair:{t}', 'title': f'{table.name}: incomplete coordinate pairs',
                    'reason': f'{partial} source rows have only one compatible coordinate. In {invalid} of those rows the other supplied coordinate is withheld as invalid under the automatic mapping; remaining rows omit it. The compatible coordinate is retained without constructing a point. Withheld values are identified by source row in the report.',
                    'table': t, 'terms': pair, 'rows': partial, 'invalid_pairs': invalid})
        if family in {'germplasm-score', 'germplasm-trait'}:
            for n, row in enumerate(table.rows):
                source = _source(table.terms, row)
                if family == 'germplasm-score':
                    missing = not source.get(DWC + 'measurementValue', '').strip() or not (
                        source.get(DWC + 'measurementType', '').strip() or source.get(G + 'measurementTraitName', '').strip())
                    reason = 'A Score row needs a supplied measurement value and type (or trait name). This incomplete row stays in originals.'
                else:
                    missing = not any(source.get(term, '').strip() for term in (
                        G + 'measurementTraitName', DWC + 'measurementMethod', G + 'measurementTraitSource', G + 'measurementTraitRemarks'))
                    reason = 'A Trait Descriptor needs a name, method, source or remarks. An identifier alone cannot create a protocol description.'
                if missing:
                    row_issues.append(_issue(f'row:{t}:{n}', f'{table.name}, row {n + 1}: incomplete description', reason, [PRESERVE], table=t, row=n + 1))
        if family == 'eol-media':
            for n, source in enumerate(sources):
                reason = eol_media_row_review(source)
                if reason:
                    options = [PRESERVE]
                    if source.get(DCT + 'type', '').strip().lower() not in TEXT_TYPES:
                        options.append({'value': 'convert', 'label': 'Convert with the chosen subject; retain missing or unsupported details'})
                    row_issues.append(_issue(f'row:{t}:{n}', f'{table.name}, row {n + 1}: media meaning', reason, options, table=t, row=n + 1))
        if family == 'eol-reference':
            for n, source in enumerate(sources):
                if not source.get(DCT + 'identifier', '').strip():
                    row_issues.append(_issue(f'row:{t}:{n}', f'{table.name}, row {n + 1}: reference identifier is missing',
                        'EOL requires a supplied reference identifier; this row stays in originals.', [PRESERVE], table=t, row=n + 1))
        if family in {'bmde', 'nbn'}:
            for n, source in enumerate(sources):
                reason = legacy_row_review(table.row_type, source)
                if reason:
                    options = [PRESERVE]
                    if family != 'bmde' or not (source.get(BMDE + 'LastModifiedAction', '').strip().upper() == 'DELETE' or source.get(BMDE + 'NoObservations', '').strip() == 'NoObs'):
                        options.append({'value': 'convert', 'label': 'Confirm handling and convert approved values; retain invalid or unsupported dates'})
                    row_issues.append(_issue(f'row:{t}:{n}', f'{table.name}, row {n + 1}: record handling', reason, options, table=t, row=n + 1))
        if family == 'germplasm-score' and G + 'measurementTraitID' in table.terms:
            issues.append(_issue(f'trait-link:{t}', f'{table.name}: link scores to trait protocols?',
                'A trait ID may be an ontology identifier. Link only when each supplied ID exactly matches one converted Trait Descriptor protocol ID; no protocol is created from a score.',
                [PRESERVE, {'value': 'exact', 'label': 'Link each supplied trait ID to exactly one converted protocol'}], table=t))
        if family == 'humboldt':
            issues.append(_issue(f'hum-category:{t}', f'{table.name}: confirm survey events',
                'A Humboldt survey needs a survey event. Missing source categories may be filled only by explicit confirmation; supplied contradictory categories are never overwritten.',
                [{'value': 'require', 'label': 'Events must already be classified as surveys'},
                 {'value': 'confirm', 'label': 'Confirm these events are surveys; fill unsupplied categories'}], table=t))
            for n, source in enumerate(sources):
                review = scope_review(source)
                if review:
                    # Grouping by identical scope claims keeps one completeness assertion to one claim.
                    row_issues.append(_issue(f'hum-scope:{t}:{n}', f'{table.name}, row {n + 1}: survey scope',
                        review['reason'], review['options'], table=t, row=n + 1,
                        scope_values={term: value for term, value in sorted(source.items()) if 'Scope' in term}))
        if table.is_core and own == "event" and (DWC + "eventCategory" not in table.terms or any(not row[table.terms.index(DWC + "eventCategory")] for row in table.rows)):
            humboldt_present = any(extension.row_type in HUMBOLDT_FAMILIES for extension in archive.tables if not extension.is_core)
            issues.append(_issue("event-category", "What kind of events are these?", f"Some events in {table.name} don't say what kind of event they are. "
                "Choose survey events if they are planned sampling or monitoring visits, or observation context if they only record where and when observations were made."
                + (' The survey descriptions (Humboldt) can only be converted for survey events; otherwise they stay in your original files.' if humboldt_present else ''),
                [{"value": "survey", "label": "Survey events (planned sampling or monitoring)"}, {"value": "occurrence", "label": "Observation context (where and when records were made)"}]))
        if own == "occurrence" and (table.is_core or family == "occurrence"):
            quantity = DWC + 'organismQuantity'
            quantity_type = DWC + 'organismQuantityType'
            unpaired = sum(bool(source.get(quantity)) != bool(source.get(quantity_type))
                           for row in table.rows for source in [_source(table.terms, row)])
            if unpaired:
                warnings.append({'id': f'quantity-pair:{t}', 'title': f'{table.name}: incomplete quantity descriptions',
                    'reason': f'{unpaired} rows supply only a quantity or its type. Values remain as supplied; no unit, organism count or abundance interpretation is inferred.',
                    'table': t, 'rows': unpaired})
            missing = DWC + "occurrenceStatus" not in table.terms or any(not row[table.terms.index(DWC + "occurrenceStatus")] for row in table.rows)
            if missing:
                zero_quantities = sum(not source.get(DWC + 'occurrenceStatus') and any(
                    re.fullmatch(r'[+-]?0+(?:\.0*)?(?:[eE][+-]?[0-9]+)?', source.get(term, '').strip())
                    for term in (quantity, DWC + 'individualCount'))
                    for row in table.rows for source in [_source(table.terms, row)])
                convention = None if zero_quantities else _specimen_presence(table)
                issues.append(_issue(f"status:{t}", f"{table.name}: were these organisms present or absent?",
                    "A Data Package needs every occurrence to say whether the organism was present or absent, and some rows don't say. Supplied values are kept; this choice only fills the empty ones."
                    + (f' {zero_quantities} of those rows have a zero count. A zero alone doesn\'t prove absence, so please choose deliberately.' if zero_quantities else ''),
                    [{"value": "present", "label": "Present: the organism was recorded"}, {"value": "absent", "label": "Absent: it was looked for but not found"}],
                    **({'convention': convention} if convention else {})))
    # Name-only values in any column that can map to an agent role link to one agent
    # per exact name by default: one automatic choice for all names, and one per
    # repeated name, each of which can keep names as text only. Every such column is
    # counted, so a remapped column's names have a choice too.
    agent_names = Counter()
    for column in columns:
        targets = {column['default'], *(option['value'] for option in column['options'])}
        if not any('.' in target and tuple(target.split('.', 1)) in ROLE_FIELDS for target in targets):
            continue
        table = archive.tables[column['table']]
        id_term = column['term'] + 'ID'
        id_column = table.terms.index(id_term) if id_term in table.terms else None
        for row in table.rows:
            # A missing-value token such as "NA" in the ID column is an empty cell.
            if id_column is not None and row[id_column] and not missing_reference(row[id_column]):
                continue
            name = agent_name(row[column['column']])
            if name and composite_name_reason(name) is None:
                agent_names[name] += 1
    if agent_names:
        issues.append(_issue(AGENT_NAMES_ID, 'People and organizations named without identifiers',
            f'{sum(agent_names.values())} mentions in name columns give a name without an identifier ({len(agent_names)} different names). '
            'Each exact name can become one Agent linked to all its mentions, or every name can stay as text only.',
            [{'value': 'shared', 'label': 'One Agent per exact name, linked to each mention'},
             {'value': 'text', 'label': 'Keep all names without identifiers as text only'}],
            kind='agent-identity', count=sum(agent_names.values()), names=len(agent_names)))
    for name, count in sorted(agent_names.items()):
        if count < 2:
            continue
        issues.append(_issue(_agent_name_id(name), f'Does {name[:100]!r} name one agent throughout?',
            f'{count} source mentions use this exact name. By default they share one Agent. '
            'Keep the name as text only if it covers different people or organizations.',
            [{'value': 'shared', 'label': 'Use one Agent for this exact name'},
             {'value': 'separate', 'label': 'Keep this name as text only, without an Agent'}],
            kind='agent-identity', source_value=name, count=count))
    plan = {"version": RULE_VERSION, "source_sha256": archive.fingerprint, "schema": dwc_dp_schema_snapshot(),
            "tables": profiles, "columns": columns, "issues": issues,
            "files": [{"name": name, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)} for name, content in sorted(archive.files.items())]}
    if getattr(archive, 'tidy', None):
        plan['tidy'] = {'version': archive.tidy['version'], 'sha256': archive.tidy['sha256']}
    plan['uploads'] = [{'name': name, 'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content)} for name, content in sorted(archive.uploaded_files.items())]
    if hierarchy_unsupported or hierarchy:
        plan['event_hierarchy'] = {'identifier': DWC + 'eventID', 'parent_identifier': PARENT,
            'basis': 'source Event rows' if core.row_type == DWC + 'Event' else 'occurrence rows combined by supplied eventID',
            **({'unsupported': hierarchy_unsupported} if hierarchy_unsupported else hierarchy_summary(hierarchy))}
    if scientific is not None:
        plan['scientific_hierarchy'] = _scientific_summary(scientific)
    _preflight_plan(archive, core, plan, row_issues)
    _streamline_plan(archive, core, plan, warnings)
    _require_valid_defaults(plan)
    apply_policy(plan['issues']); apply_policy(plan['automatic_choices'])
    plan["id"] = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    return plan


def _preflight_plan(archive, core, plan, row_issues):
    """Attach precomputed option requirements, unavailable options and grouped row issues."""
    checks = preflight(archive, core, plan['columns'], plan['issues'], row_issues)
    grouped, members = group_rows(row_issues, [table.name for table in archive.tables])
    plan['issues'].extend(grouped)
    plan['row_issues'] = members
    remove_unavailable([*plan['issues'], *plan['columns']], checks.unavailable)
    unavailable_tables = dict(checks.unavailable_tables)
    for issue in plan['issues']:
        # A dependent question with no possible answer makes its extension unconvertible.
        if not issue['options'] and 'table' in issue and issue['id'] != f"table:{issue['table']}":
            unavailable_tables.setdefault(issue['table'], ' '.join(option['reason'] for option in issue.get('unavailable_options', [])))
    for t, reason in unavailable_tables.items():
        issue = next(issue for issue in plan['issues'] if issue['id'] == f'table:{t}')
        issue.update(options=[PRESERVE], reason=reason)
        plan['tables'][t]['conversion_unavailable'] = reason
    plan['requirements'] = {decision: {value: list(requirements) for value, requirements in sorted(values.items())}
                            for decision, values in sorted(checks.requirements.items())}


def _require_valid_defaults(plan):
    """An automatic choice whose requirements fail under the automatic defaults needs input instead."""
    while True:
        effective = effective_decisions(plan, {})
        failing = [choice for choice in plan['automatic_choices']
                   if failed_requirements(plan, effective, choice['id'], choice['default'])]
        if not failing:
            return
        for choice in failing:
            reasons = ' '.join(requirement['reason'] for requirement in failed_requirements(plan, effective, choice['id'], choice['default']))
            plan['automatic_choices'].remove(choice)
            plan['warnings'] = [warning for warning in plan['warnings'] if warning['id'] != choice['id']]
            question = {key: value for key, value in choice.items() if key not in {'default', 'question_reason'}}
            plan['issues'].append(question | {'reason': choice.get('question_reason', choice['reason']) + ' ' + reasons})
            for column in plan['columns']:
                if column['id'] == choice['id']:
                    column['review'] = True


def validate_decisions(plan, decisions, require_complete=True):
    """The only gate for every decision source. Returns unresolved and violating decision ids.

    Partial saves accept any offered option and report requirement violations as
    unresolved; a complete check rejects missing choices and every active
    effective choice (explicit, automatic, group-expanded or column default)
    whose requirements fail.
    """
    if not isinstance(decisions, dict) or any(not isinstance(value, str) for value in decisions.values()):
        raise ConversionError("Decisions must be a mapping of decision IDs to option strings.", category='decision')
    entries = [*plan["columns"], *plan["issues"], *plan.get('automatic_choices', [])]
    choices = {item["id"]: {option["value"] for option in item["options"]} for item in entries}
    for member in plan.get('row_issues', []):
        choices[member['id']] = choices.get(member['group'], set())
    for index, table in enumerate(plan['tables']):
        if table.get('conversion_unavailable') and decisions.get(f'table:{index}', 'preserve') != 'preserve':
            raise ConversionError(table['conversion_unavailable'], category='decision', decision_ids=[f'table:{index}'])
    for item in plan["columns"]:
        if item.get("parent_link_unavailable") and decisions.get(item["id"]) == PARENT_LINK:
            raise ConversionError(item["parent_link_unavailable"], category='decision', decision_ids=[item['id']])
    for key, value in decisions.items():
        if key not in choices or value not in choices[key]:
            raise ConversionError(f"Unsupported mapping decision: {key}={value!r}.", category='decision', decision_ids=[key])
    effective = effective_decisions(plan, decisions)
    nested = plan.get('taxonomy', {}).get('occurrence_plans', {})
    nested_ids = {f'taxon-occurrence:{index}:' + issue['id'] for index, inner in nested.items() for issue in inner['issues']}

    def resolved(item):
        # A group is resolved when every member has an effective choice, even without a group key.
        return all(member in effective for member in item['members']) if item.get('members') else item['id'] in effective

    unresolved = [item["id"] for item in plan["issues"] if item['id'] not in nested_ids and not resolved(item) and not (
        item.get('source_column') is not None and effective.get(f"column:{item['table']}:{item['source_column']}") == 'preserve') and not (
        "table" in item and (effective.get(f"table:{item['table']}") == "preserve" or
                            ('row' in item and effective.get(f"row:{item['table']}:{item['row'] - 1}") == 'preserve')))]
    found = violations(plan, decisions)
    active_nested = []
    for index, inner in nested.items():
        if effective.get(f'table:{index}') == 'preserve':
            continue
        prefix = f'taxon-occurrence:{index}:'
        inner_decisions = {key[len(prefix):]: value for key, value in decisions.items() if key.startswith(prefix)}
        unresolved.extend(prefix + key for key in validate_decisions(inner, inner_decisions, require_complete=False))
        active_nested.append((prefix, inner, inner_decisions))
    if require_complete and found:
        raise ConversionError(' '.join(dict.fromkeys(reason for item in found for reason in item['reasons'])), category='decision',
                              decision_ids=[identifier for item in found for identifier in item['decision_ids']],
                              evidence={'violations': found[:10]})
    if require_complete and unresolved:
        raise ConversionError(f"Resolve {len(unresolved)} review decisions before converting.", category='decision',
                              decision_ids=unresolved)
    if require_complete:
        for prefix, inner, inner_decisions in active_nested:
            try:
                validate_decisions(inner, inner_decisions)
            except ConversionError as error:
                raise ConversionError(str(error), category=error.category, decision_ids=[prefix + key for key in error.decision_ids],
                                      evidence=error.evidence) from error
    return list(dict.fromkeys([*unresolved, *(item['id'] for item in found)]))


def _shared_event_ids(core, core_columns, decisions):
    """{eventID: parent eventCategory} for supplied eventIDs that several per-row events would repeat.

    Common missing-value tokens are not identities and are never grouped. The parent's
    category is the one its rows agree on, otherwise the generic occurrence context.
    """
    if core.row_type != DWC + 'Occurrence' or decisions.get('event-grain') in COMBINED_GRAINS:
        return {}

    def column(target):
        return next((item['column'] for item in core_columns if decisions.get(item['id'], item['default']) == target), None)
    id_column, category_column = column('event.eventID'), column('event.eventCategory')
    if id_column is None:
        return {}
    categories = defaultdict(list)
    for row in core.rows:
        if row[id_column] and not missing_reference(row[id_column], ()):
            categories[row[id_column]].append(row[category_column] if category_column is not None else '')
    return {event_id: (supplied[0] if supplied[0] and len(set(supplied)) == 1 else 'occurrence')
            for event_id, supplied in categories.items() if len(supplied) > 1}


def _link_parents(archive, core, core_index, plan, decisions, core_columns, event_keys, events_by_key, column_consumption):
    """Emit supplied parentEvent_fk links after all core events exist, so forward references resolve.

    event_keys map source rows to the event their eventID identifies, never to a depth child event.
    """
    column = next((item for item in core_columns if item['term'] == PARENT), None)
    if column is None:
        return None
    decision = decisions.get(column['id'], column['default'])
    nodes, unsupported, result = _hierarchy(archive, core)
    scientific = _scientific_hierarchy(archive, core, nodes, result)
    links = {}
    if decision == PARENT_LINK:
        if core.row_type == DWC + 'Occurrence' and decisions.get('event-grain') not in COMBINED_GRAINS:
            raise ConversionError('Parent event links on an Occurrence core require events combined by supplied eventID; '
                                  'separate per-row events have no established persistent identity. Preserve parentEventID instead.',
                                  category='decision', decision_ids=['event-grain', column['id']])
        if unsupported or not result['links']:
            raise ConversionError(unsupported or (describe_problems(result) if result['problems'] else 'No supplied parentEventID identifies a source event.'),
                                  category='decision', decision_ids=[column['id']])
        for child, parent in result['links'].items():
            child_key, parent_key = event_keys[nodes[child]['join_ids'][0]], event_keys[nodes[parent]['join_ids'][0]]
            events_by_key[child_key]['parentEvent_fk'] = parent_key
            links[child_key] = (parent_key, nodes[parent]['rows'][0])
            for row in nodes[child]['rows']:
                column_consumption[(core_index, PARENT)].add(row - 1)
    parent_index = core.terms.index(PARENT)
    id_index = core.terms.index(DWC + 'eventID') if DWC + 'eventID' in core.terms else None
    known_ids = {row[id_index] for row in core.rows if row[id_index]} if id_index is not None else set()
    problem_by_row = {number: problem['problem'] for problem in (result or {}).get('problems', [])
                      for number in problem['rows']}
    source_values = []
    for n, (row, source_id) in enumerate(zip(core.rows, core.ids)):
        if not row[parent_index]:
            continue
        child_key = event_keys[source_id]
        parent_key, parent_row = links.get(child_key, ('', None))
        withheld_reason = ('' if parent_key else 'empty-reference-token' if missing_reference(row[parent_index], known_ids)
                           else problem_by_row.get(n + 1, 'preserved-by-decision'))
        source_values.append({'source_table': core.name, 'source_table_index': core_index, 'source_row': n + 1, **core.row_sources[n],
                              'archive_join_id': source_id, 'eventID': row[id_index] if id_index is not None else '',
                              'parentEventID': row[parent_index], 'event_key': child_key,
                              'parent_event_key': parent_key, 'parent_source_row': parent_row,
                              'status': 'linked' if parent_key else 'retained in originals',
                              **({'withheld_reason': withheld_reason} if withheld_reason else {})})
    return {'decision': decision, 'identifier': DWC + 'eventID', 'parent_identifier': PARENT,
            **({'unsupported': unsupported} if unsupported else hierarchy_summary(result)),
            'linked_events': len(links), 'source_values': source_values,
            **({'scientific_consistency': {**scientific, 'decision': decision,
                'supplied_links_retained': decision == PARENT_LINK}}
               if scientific is not None else {}),
            'policy': 'Links join supplied parentEventID to a unique persistent eventID only. No parent events are created, '
                      'and no values, observations, scopes, absences or categories are inherited or rolled up. '
                      'On Occurrence cores, event records use the source occurrence rows\' own event fields; no separate source Event record is inferred.'}


def convert(archive, plan, decisions):
    if build_plan(archive)["id"] != plan["id"]:
        raise ConversionError("The mapping plan no longer matches these files or mapping rules. Inspect the upload again.", category='stale-plan')
    validate_decisions(plan, decisions)
    user_decisions = dict(decisions)
    decisions = effective_decisions(plan, decisions)
    if next(table for table in archive.tables if table.is_core).row_type == DWC + 'Taxon':
        from api.dwca_taxon import convert_taxon
        return convert_taxon(archive, plan, decisions, user_decisions=user_decisions)
    resources = defaultdict(list); crosswalk = []; dispositions = []; material_groups = {}; material_evidence = defaultdict(set); sequence_groups = {}; protocol_groups = {}; occurrences_by_identifier = defaultdict(list); media_groups = defaultdict(dict); media_subjects = []; core_index = next(i for i,t in enumerate(archive.tables) if t.is_core)
    events_by_key = {}; supplied_categories = set(); humboldt_groups = {}; column_consumption = defaultdict(set); withheld_values = []; preserved_rows = []
    materials_by_source = {}; material_identifiers = defaultdict(set); trait_protocols = defaultdict(list); skipped_by_table = defaultdict(set); extension_subjects = []; typed_withheld = defaultdict(set)
    reviewed_target_rows = defaultdict(set)
    # Missing-value tokens (NA, null, ...) in identifier fields are empty cells, counted once per column, not withheld values.
    placeholder_rows = defaultdict(set); qualifier_retained = defaultdict(Counter)
    core = archive.tables[core_index]; event_keys = {}; occurrence_keys = {}
    per_row_events = core.row_type == DWC + 'Occurrence' and decisions.get('event-grain') not in COMBINED_GRAINS
    namespace = archive.fingerprint
    columns_by_table = defaultdict(list)
    issues_by_id = {issue['id']: issue for issue in [*plan['issues'], *plan.get('automatic_choices', [])]}
    issues_by_id.update({member['id']: {**issues_by_id[member['group']], **member} for member in plan.get('row_issues', [])})
    reviewed_value_issues = {(issue['table'], issue['source_column'], issue['source_value']): issue
                             for issue in [*plan['issues'], *plan.get('automatic_choices', [])]
                             if issue['id'].startswith(('country-label:', 'age-remark:'))}
    reviewed_value_columns = {(t, c) for t, c, _ in reviewed_value_issues}
    for column in plan["columns"]:
        columns_by_table[column["table"]].append(column)

    def values(t, row, n):
        result = defaultdict(dict)
        for column in columns_by_table[t]:
            target = decisions.get(column["id"], column["default"])
            if target in {"preserve", "join", 'derive', PARENT_LINK}:
                continue
            value = row[column['column']]
            reviewed_issue = reviewed_value_issues.get((t, column['column'], value)) if value else None
            if reviewed_issue:
                target = decisions[reviewed_issue['id']]
                if target == 'preserve':
                    continue
            if target.startswith('material.') and decisions.get(f'material:{t}', 'preserve') == 'preserve':
                continue
            table, field = target.split(".", 1)
            if missing_reference(value) and (target.endswith(ASSERTION_IRI_FIELDS) or (target == 'event.eventID' and per_row_events and t == core_index)):
                # A placeholder is not an IRI or an identity; separate row events never share it as an eventID.
                placeholder_rows[(t, column['column'])].add(n)
                continue
            rejection = _copy_rejection(archive.tables[t].row_type, target, value)
            if rejection:
                withheld_values.append({'source_table': archive.tables[t].name, 'source_table_index': t, 'source_row': n + 1,
                                       'term': column['term'], 'value': value, 'target': target,
                                       'reason': rejection})
                typed_withheld[(t, column['column'])].add(n)
                continue
            if field in result[table] and value and result[table][field] and result[table][field] != value:
                raise ConversionError(f'Conflicting source values map to {target}; preserve one column or correct the source.', category='conflict',
                                      decision_ids=([reviewed_issue['id']] if reviewed_issue else []) +
                                                   [item['id'] for item in columns_by_table[t] if decisions.get(item['id'], item['default']) == target],
                                      evidence={'source_table': archive.tables[t].name, 'source_row': n + 1})
            if value or field not in result[table]:
                result[table][field] = value
            if value and column['term'] in {DWC + 'countryCode', DWC + 'eventRemarks'}:
                reviewed_target_rows[(t, column['column'], target)].add(n)
        return result

    def approved_source(t, row):
        selected = [column for column in columns_by_table[t] if decisions.get(column['id'], column['default']) != 'preserve']
        return _source([column['term'] for column in selected], [row[column['column']] for column in selected])

    def patch_columns(t, field):
        return [*([f'occurrence-events:{t}'] if f'occurrence-events:{t}' in issues_by_id else []),
                *(item['id'] for item in columns_by_table[t] if decisions.get(item['id'], item['default']) in {'event.' + field, 'derive'})]

    def derived_source(t, row):
        return {column['term']: row[column['column']] for column in columns_by_table[t]
                if decisions.get(column['id'], column['default']) == 'derive'}

    def consumed_records(t, n, row, records, derived):
        emitted = {(name, field) for name, record in records for field, value in record.items() if value}
        row_type = archive.tables[t].row_type
        for column in columns_by_table[t]:
            if not row[column['column']]: continue
            target = decisions.get(column['id'], column['default'])
            if target.startswith('occurrence-assertion.') and row_type in GERMPLASM_FAMILIES:
                target = next((name for name, _ in records if name.endswith('-assertion')), 'occurrence-assertion') + '.' + target.split('.', 1)[1]
            consumed = target not in {'preserve', 'join', 'derive'} and tuple(target.split('.', 1)) in emitted
            if target == 'derive':
                term = column['term']
                if row_type in GERMPLASM_FAMILIES:
                    consumed = (term in derived and (
                        (row_type.endswith('GermplasmAccession') and any(record.get('assertionTypeIRI') == term for _, record in records)) or
                        (row_type.endswith('MeasurementScore') and any(record.get('verbatimAssertionType') == derived[term] for _, record in records)) or
                        (row_type.endswith('MeasurementTrial') and any(field in record for _, record in records for field in
                            ({GEO + 'lat': ['decimalLatitude'], GEO + 'lon': ['decimalLongitude'], GEO + 'alt': ['minimumElevationInMeters']}[term])))))
                elif row_type in LEGACY_FAMILIES:
                    consumed = term in derived and any(
                        (name == 'occurrence-assertion' and any(term in group and all(record.get(field) == derived.get(source_term) for source_term, field in group.items() if derived.get(source_term)) for group in GROUPS)) or
                        (name == 'event' and ((term in UTM and 'verbatimCoordinates' in record) or
                                             (term in TIMES and 'eventTime' in record) or (term in NBN_DATE and 'eventDate' in record)))
                        for name, record in records)
            if consumed: column_consumption[(t, column['term'])].add(n)

    def add_extension(name, record, t, n):
        if name != 'event':
            add(name, record, t, n)
            return
        # Extension event details describe the event an eventID names; depth children hold only depth.
        record = {**record, 'event_pk': depth_parents.get(record['event_pk'], record['event_pk'])}
        event = events_by_key.get(record['event_pk'])
        if event is None:
            raise ConversionError('An extension event patch must refer to an existing core event.', category='internal')
        for field, value in record.items():
            if value and event.get(field) and event[field] != value:
                raise ConversionError(f'Extension {archive.tables[t].name}, row {n + 1}, conflicts with existing event.{field}; preserve the field or correct the source.',
                                      category='conflict', decision_ids=[f'table:{t}', *patch_columns(t, field)],
                                      evidence={'source_table': archive.tables[t].name, 'source_row': n + 1, 'field': field,
                                                'event_value': event[field], 'extension_value': value})
        combined = {**event, **{field: value for field, value in record.items() if value}}
        if combined.get('year') and combined.get('eventDate'):
            if not re.fullmatch(r'-?\d{1,4}', combined['year']):
                raise ConversionError('Mapped event.year is not an integer year.', category='conflict',
                                      decision_ids=[f'table:{t}', *patch_columns(t, 'year')], evidence={'source_row': n + 1})
            if _year_disagrees(combined['year'], combined['eventDate']):
                raise ConversionError('An extension year conflicts with the existing eventDate.', category='conflict',
                                      decision_ids=[f'table:{t}', *patch_columns(t, 'year')],
                                      evidence={'source_table': archive.tables[t].name, 'source_row': n + 1,
                                                'year': combined['year'], 'eventDate': combined['eventDate']})
        event.update(combined)
        trace('event', {'event_pk': event['event_pk']}, t, n)

    def source_signature(t, row):
        return tuple(row[column['column']] for column in columns_by_table[t] if column['default'] != 'join')

    def trace(name, key, t, n, target_row=None):
        crosswalk.append({"source_table": archive.tables[t].name, "source_row": n + 1, "target_table": name,
                          'source_table_index': t, 'source_row_type': archive.tables[t].row_type,
                          **archive.tables[t].row_sources[n], "target_row": target_row,
                          "key": key,
                          "archive_join_id": archive.tables[t].ids[n] if archive.tables[t].ids else ""})

    def add(name, row, t, n):
        resources[name].append(row)
        if name == 'event': events_by_key[row['event_pk']] = row
        key_fields = TABLE_SPECS[name].primary_key or [field for field in row if field.endswith('_fk')]
        trace(name, {field: row.get(field) for field in key_fields}, t, n, len(resources[name]))

    def name_values(t, n, record):
        """Keep the supplied name text verbatim and remove an exactly matching supplied authorship."""
        table = archive.tables[t]
        name = table.rows[n][table.terms.index(NAME)] if NAME in table.terms else ''
        if name and not record.get('verbatimIdentification'):
            # The pinned DwC-DP has no identificationQualifier field, and scientificName
            # excludes qualifiers. The supplied qualifier joins the supplied name text in
            # verbatimIdentification, so "cf.", "?" or "sp." survive without changing
            # scientificName. One that cannot be placed stays in the originals.
            qualifier = table.rows[n][table.terms.index(QUALIFIER)].strip() if QUALIFIER in table.terms else ''
            authorship = table.rows[n][table.terms.index(AUTHORSHIP)] if AUTHORSHIP in table.terms else ''
            qualified = _qualified_name(name, qualifier, authorship) if qualifier else None
            if qualified is not None:
                name = qualified
                column_consumption[(t, QUALIFIER)].add(n)
            elif qualifier:
                qualifier_retained[t]['qualifier_not_placeable'] += 1
            record['verbatimIdentification'] = name
            column_consumption[(t, NAME)].add(n)
        elif QUALIFIER in table.terms and table.rows[n][table.terms.index(QUALIFIER)].strip():
            qualifier_retained[t]['verbatim_identification_supplied' if name else 'no_name_text'] += 1
        if record.get('scientificName'):
            record['scientificName'] = _without_authorship(record['scientificName'], record.get('scientificNameAuthorship', ''))
        return record

    def occurrence_row(t, n, source_id, mapped, event_key):
        # DwC individualCount is a count of organisms, which DwC-DP represents
        # as a quantity paired with its unit. It fills that pair only when neither
        # half was supplied; next to a different supplied quantity (a density, say)
        # it becomes an individualCount assertion instead. Zero remains a quantity;
        # it never changes occurrenceStatus. This applies to Occurrence cores and
        # to Occurrence extensions converted as occurrences alike.
        table = archive.tables[t]
        route, count = (_individual_count(_source(table.terms, table.rows[n]))
                        if table.row_type == DWC + 'Occurrence' and DWC + 'individualCount' in table.terms else (None, None))
        if route == 'quantity':
            mapped.setdefault('occurrence', {}).update(organismQuantity=count, organismQuantityType='individuals')
        occurrence = name_values(t, n, mapped.get("occurrence", {}))
        occurrence.update(occurrence_pk=_key(archive, "occurrence", t, source_id), event_fk=event_key)
        if not occurrence.get("occurrenceStatus"):
            occurrence["occurrenceStatus"] = decisions.get(f"status:{t}", "")
        occurrence_keys[source_id] = occurrence["occurrence_pk"]
        if occurrence.get('occurrenceID') and not missing_reference(occurrence['occurrenceID'], ()):
            occurrences_by_identifier[occurrence['occurrenceID']].append((occurrence['occurrence_pk'], event_key))
        add("occurrence", occurrence, t, n)
        if route == 'assertion':
            add('occurrence-assertion', {'occurrence_fk': occurrence['occurrence_pk'], 'assertionType': 'individualCount',
                                         'assertionTypeIRI': DWC + 'individualCount', 'assertionValue': count,
                                         'assertionUnit': 'individuals'}, t, n)
        if decisions.get(f'material:{t}', 'preserve') != 'preserve':
            material = dict(mapped.get('material', {}))
            group = (t, source_id) if decisions[f'material:{t}'] == 'per_row' else (t, material.get('materialEntityID', ''))
            if not group[1] or (decisions[f'material:{t}'] == 'by_id' and missing_reference(group[1], ())):
                raise ConversionError('Combining material rows requires a usable mapped material identifier on every row.', category='decision',
                                      decision_ids=[f'material:{t}'])
            # evidenceForOccurrenceID shares a source IRI with occurrenceID, but is a relationship, not a copied identifier.
            material.pop('evidenceForOccurrenceID', None)
            material.update(materialEntity_pk=_key(archive, 'material', *group), collectionEvent_fk=event_key)
            if group in material_groups and material_groups[group] != material:
                raise ConversionError(f'Conflicting material values or collection events for {group[1]!r}; retain separate records or correct the source.',
                                      category='conflict', decision_ids=[f'material:{t}', 'event-grain'],
                                      evidence={'source_table': archive.tables[t].name, 'source_row': n + 1, 'materialEntityID': group[1]})
            if group not in material_groups:
                material_groups[group] = material; add('material', material, t, n)
            else:
                trace('material', {'materialEntity_pk': material['materialEntity_pk']}, t, n)
            if occurrence.get('occurrenceID') and not missing_reference(occurrence['occurrenceID'], ()):
                material_evidence[group].add(occurrence['occurrenceID'])
            materials_by_source[source_id] = material
            if material.get('materialEntityID'):
                material_identifiers[material['materialEntity_pk']].add(material['materialEntityID'])
        if any(mapped.get('identification', {}).values()):
            record = {**mapped["identification"], "identification_pk": _key(archive, "identification-core", t, source_id), "occurrence_fk": occurrence["occurrence_pk"]}
            if record.get('scientificName'):
                record['scientificName'] = _without_authorship(record['scientificName'], record.get('scientificNameAuthorship', '')
                                                               or occurrence.get('scientificNameAuthorship', ''))
            # Copy taxon name context into the classification record, without marking it accepted.
            for field in ("scientificName", "scientificNameID", "taxonID", "verbatimIdentification"):
                if occurrence.get(field): record.setdefault(field, occurrence[field])
            add("identification", record, t, n)

    event_groups = {}; group_keys = {}; depth_children = defaultdict(set); depth_parents = {}
    depth_split = core.row_type == DWC + "Occurrence" and decisions.get("event-grain") == DEPTH_SPLIT
    # Separate per-row events never repeat a supplied eventID. A repeated eventID
    # identifies one event that contains the row events, as for depth children.
    shared_ids, shared_children = _shared_event_ids(core, columns_by_table[core_index], decisions), defaultdict(int)
    for n, (row, source_id) in enumerate(zip(core.rows, core.ids)):
        mapped = values(core_index, row, n); event = mapped.get("event", {})
        # Depth stays with each depth's child event; the combined event carries no depth range.
        depth = {field: event.pop(field) for field in DEPTH_FIELDS if field in event} if depth_split else {}
        if event.get('eventID') in shared_ids:
            shared = event.pop('eventID')
            parent_key = _key(archive, 'event-shared-id', shared)
            if parent_key in events_by_key:
                trace('event', {'event_pk': parent_key}, core_index, n)
            else:
                # Only the supplied identity and a category; no row's details are combined or inherited.
                add('event', {'event_pk': parent_key, 'eventID': shared, 'eventCategory': shared_ids[shared]}, core_index, n)
            event['parentEvent_fk'] = parent_key
            shared_children[parent_key] += 1
        group_id = source_id
        if core.row_type == DWC + "Occurrence" and decisions["event-grain"] in COMBINED_GRAINS:
            group_id = event.get("eventID", "")
            if not group_id or missing_reference(group_id, ()):
                raise ConversionError("Combining events requires a usable eventID on every core row.", category='decision',
                                      decision_ids=['event-grain'])
        event_key = _key(archive, "event", group_id); event_keys[source_id] = group_keys[source_id] = event_key
        event.update(event_pk=event_key)
        if not event.get("eventCategory"):
            event["eventCategory"] = "occurrence" if core.row_type == DWC + "Occurrence" else decisions.get("event-category", "")
        if DWC + 'eventCategory' in core.terms and row[core.terms.index(DWC + 'eventCategory')]:
            supplied_categories.add(event_key)
        if group_id in event_groups:
            if event_groups[group_id] != event:
                raise ConversionError(f"Conflicting event values for eventID {group_id!r}; use separate events or correct the source.",
                                      category='conflict', decision_ids=['event-grain', *(item['id'] for item in columns_by_table[core_index]
                                                                                        if decisions.get(item['id'], item['default']).startswith('event.'))],
                                      evidence={'eventID': group_id, 'source_row': n + 1,
                                                'fields': sorted(field for field in {*event, *event_groups[group_id]}
                                                                 if event.get(field) != event_groups[group_id].get(field))})
            trace('event', {'event_pk': event_key}, core_index, n)
        else:
            event_groups[group_id] = event; add("event", event, core_index, n)
        if depth_split:
            # One child per distinct source depth; no eventID is invented for it.
            child_key = _key(archive, "event-depth", group_id, *_depth_key(core, row))
            if child_key in events_by_key:
                trace('event', {'event_pk': child_key}, core_index, n)
            else:
                child = {field: value for field, value in depth.items() if value}
                child.update(event_pk=child_key, parentEvent_fk=event_key, eventCategory=event["eventCategory"])
                add("event", child, core_index, n)
            depth_children[event_key].add(child_key); depth_parents[child_key] = event_key
            event_key = event_keys[source_id] = child_key
        if core.row_type == DWC + "Occurrence":
            occurrence_row(core_index, n, source_id, mapped, event_key)

    hierarchy_report = _link_parents(archive, core, core_index, plan, decisions, columns_by_table[core_index],
                                     group_keys, events_by_key, column_consumption)

    extension_order = sorted(enumerate(archive.tables), key=lambda item:
        0 if item[1].row_type == DWC + 'Occurrence' else
        1 if SUPPORTED_EXTENSIONS.get(item[1].row_type) in {'germplasm-accession', 'germplasm-trait'} else
        3 if SUPPORTED_EXTENSIONS.get(item[1].row_type) == 'germplasm-score' else 2)
    for t, table in extension_order:
        if table.is_core: continue
        role = decisions[f"table:{t}"]
        if role == "preserve": continue
        if role == 'humboldt-grouped':
            if decisions.get('event-grain') != 'by_id':  # Depth children would split each surveyed event.
                raise ConversionError('Occurrence-core Humboldt surveys require events combined by supplied eventID after consistency checks.',
                                      category='decision', decision_ids=['event-grain', f'table:{t}'])
            expected, attached, signatures = defaultdict(set), defaultdict(set), defaultdict(set)
            for source_id in core.ids: expected[event_keys[source_id]].add(source_id)
            for row, source_id in zip(table.rows, table.ids):
                event_key = event_keys[source_id]; attached[event_key].add(source_id); signatures[event_key].add(source_signature(t, row))
            if any(attached[key] != expected[key] or len(signatures[key]) != 1 for key in attached):
                raise ConversionError('Grouped Humboldt rows need complete identical coverage of every occurrence in each attached event.',
                                      category='conflict', decision_ids=[f'table:{t}'])
        for n, (row, source_id) in enumerate(zip(table.rows, table.ids)):
            try:
                if decisions.get(f'row:{t}:{n}') == 'preserve':
                    preserved_rows.append({'source_table': table.name, 'source_table_index': t, 'source_row': n + 1,
                                           'reason': issues_by_id[f'row:{t}:{n}']['reason']})
                    skipped_by_table[t].add(n)
                    continue
                mapped = values(t, row, n)
                if role == "occurrence":
                    event_key = event_keys[source_id]
                    details = decisions.get(f'occurrence-events:{t}', 'preserve')
                    supplied = {field: value for field, value in mapped.get('event', {}).items() if value}
                    if details == 'per-row':
                        # Each row is its own event inside the linked event; the shared eventID stays with the parent.
                        child = {field: value for field, value in supplied.items() if field != 'eventID'}
                        child.update(event_pk=_key(archive, 'occurrence-event', t, n), parentEvent_fk=event_key)
                        child.setdefault('eventCategory', 'occurrence')  # A supplied category is kept, never replaced.
                        add('event', child, t, n)
                        event_key = child['event_pk']
                        consumed_fields = set(child)
                    elif details == 'patch' and supplied:
                        add_extension('event', {'event_pk': event_key, **supplied}, t, n)
                        consumed_fields = set(supplied)
                    else:
                        consumed_fields = set()
                    for column in columns_by_table[t]:
                        target = decisions.get(column['id'], column['default'])
                        if target.startswith('event.') and target.split('.', 1)[1] in consumed_fields and row[column['column']]:
                            column_consumption[(t, column['term'])].add(n)
                    occurrence_row(t, n, f"{source_id}:{n}", mapped, event_key)
                elif role == "identification":
                    # Each identification keeps its own name text, never its occurrence's.
                    record = name_values(t, n, {**mapped.get("identification", {}), "identification_pk": _key(archive, "identification", t, n),
                                                "occurrence_fk": occurrence_keys[source_id]})
                    add("identification", record, t, n)
                elif role.endswith("-assertion") or role == 'declared-assertions':
                    record = dict(mapped.get("occurrence-assertion", {}))
                    explicit_id = row[table.terms.index(DWC + 'occurrenceID')] if DWC + 'occurrenceID' in table.terms else ''
                    if missing_reference(explicit_id, occurrences_by_identifier):
                        explicit_id = ''
                    if role == 'declared-assertions' and explicit_id:
                        matches = occurrences_by_identifier.get(explicit_id, [])
                        if len(matches) != 1 or matches[0][1] != event_keys[source_id]:
                            raise ConversionError(f'Assertion occurrenceID {explicit_id!r} does not resolve to exactly one occurrence in its attached event.',
                                                  category='conflict', decision_ids=[f'table:{t}'],
                                                  evidence={'source_table': table.name, 'source_row': n + 1, 'matches': len(matches)})
                        record['occurrence_fk'] = matches[0][0]; target = 'occurrence-assertion'
                    elif role == 'occurrence-assertion':
                        if explicit_id:
                            matches = occurrences_by_identifier.get(explicit_id, [])
                            if len(matches) != 1 or matches[0][0] != occurrence_keys[source_id]:
                                raise ConversionError('The assertion occurrenceID disagrees with its archive core attachment.', category='conflict',
                                                      decision_ids=[f'table:{t}'], evidence={'source_table': table.name, 'source_row': n + 1})
                        record['occurrence_fk'] = occurrence_keys[source_id]; target = role
                    else:
                        record['event_fk'] = event_keys[source_id]; target = 'event-assertion'
                    add(target, record, t, n)
                elif role == "resource-relationship":
                    record = mapped.get(role, {})
                    add(role, record, t, n)
                elif role in {'identifier', 'reference', 'eol-reference'}:
                    subject_table = 'event' if core.row_type == DWC + 'Event' else 'occurrence'
                    subject_key = event_keys[source_id] if subject_table == 'event' else occurrence_keys[source_id]
                    records = (emit_eol_records(role, _source(table.terms, row), mapped, subject_table, subject_key, _key(archive, 'reference', t, n))
                               if role == 'eol-reference' else emit_reference_records(role, mapped, subject_table, subject_key, _key(archive, 'reference', t, n)))
                    for name, record in records:
                        add(name, record, t, n)
                elif role.startswith('humboldt-'):
                    event_key = event_keys[source_id]; event = events_by_key[event_key]
                    if event['eventCategory'] != 'survey':
                        if decisions[f'hum-category:{t}'] != 'confirm' or event_key in supplied_categories:
                            raise ConversionError('Humboldt rows need reviewed survey events; a supplied non-survey eventCategory cannot be overwritten.',
                                                  category='decision', decision_ids=[f'hum-category:{t}', 'event-category', f'table:{t}'])
                        event['eventCategory'] = 'survey'
                    source = approved_source(t, row)
                    scope_decision = decisions.get(f'hum-scope:{t}:{n}', 'source')
                    group = (t, event_key) if role in {'humboldt-grouped', 'humboldt-merge'} else (t, n)
                    signature = (source_signature(t, row), json.dumps(mapped, sort_keys=True), scope_decision)
                    if group in humboldt_groups and humboldt_groups[group][0] != signature:
                        first_row = humboldt_groups[group][2]
                        raise ConversionError('Combining Humboldt surveys requires identical source values and scope decisions; retain separate surveys instead.',
                                              category='conflict', decision_ids=[f'table:{t}', f'hum-scope:{t}:{first_row}', f'hum-scope:{t}:{n}'],
                                              evidence={'source_table': table.name, 'source_rows': [first_row + 1, n + 1]})
                    survey_key = _key(archive, 'survey', *group)
                    records, consumed, withheld = emit_humboldt_records(
                        source, mapped, event_key, survey_key, scope_decision,
                        original_source=_source(table.terms, row))
                    for name, record in records:
                        if group in humboldt_groups:
                            keys = TABLE_SPECS[name].primary_key or [field for field in record if field.endswith('_fk')]
                            trace(name, {field: record.get(field) for field in keys}, t, n)
                        else:
                            add(name, record, t, n)
                    humboldt_groups.setdefault(group, (signature, survey_key, n))
                    for term in consumed: column_consumption[(t, term)].add(n)
                    withheld_values.extend({'source_table': table.name, 'source_row': n + 1, 'term': term,
                                            'value': source[term], 'reason': reason} for term, reason in withheld.items())
                elif role.startswith('germplasm-'):
                    family = SUPPORTED_EXTENSIONS[table.row_type]; derived = derived_source(t, row)
                    raw = _source(table.terms, row)
                    subject_table = role.rsplit('-', 1)[-1] if family == 'germplasm-score' else {'germplasm-accession': 'material', 'germplasm-trait': 'protocol', 'germplasm-trial': 'event'}[family]
                    if subject_table == 'material':
                        material = materials_by_source.get(source_id)
                        if not material or not material.get('materialEntityID'):
                            raise ConversionError('Germplasm material conversion requires an explicitly approved material with a supplied identifier on every linked core row.',
                                                  category='conflict', decision_ids=[f'table:{t}', f'material:{core_index}'],
                                                  evidence={'source_table': table.name, 'source_row': n + 1})
                        subject_key = material['materialEntity_pk']
                        if family == 'germplasm-score' and (not raw.get(G + 'germplasmID') or raw[G + 'germplasmID'] not in material_identifiers[subject_key]):
                            raise ConversionError('Score germplasmID must exactly match an identifier of its reviewed material subject.',
                                                  category='conflict', decision_ids=[f'table:{t}', f'material:{core_index}'],
                                                  evidence={'source_table': table.name, 'source_row': n + 1, 'germplasmID': raw.get(G + 'germplasmID', '')})
                    elif subject_table == 'protocol': subject_key = _key(archive, 'trait-protocol', t, n)
                    elif subject_table == 'event': subject_key = event_keys[source_id]
                    else: subject_key = occurrence_keys[source_id]
                    records = emit_germplasm_records(family, derived, mapped, subject_table, subject_key, _key(archive, 'germplasm-reference', t, n))
                    if family == 'germplasm-score' and decisions.get(f'trait-link:{t}') == 'exact' and raw.get(G + 'measurementTraitID'):
                        matches = trait_protocols[raw[G + 'measurementTraitID']]
                        if len(matches) != 1:
                            raise ConversionError('A Score trait ID must exactly match one converted Trait Descriptor protocol ID; missing or duplicate matches cannot be linked.',
                                                  category='conflict', decision_ids=[f'trait-link:{t}'],
                                                  evidence={'source_table': table.name, 'source_row': n + 1, 'traitID': raw[G + 'measurementTraitID']})
                        for _, record in records: record['assertionProtocol_fk'] = matches[0]
                        column_consumption[(t, G + 'measurementTraitID')].add(n)
                    extension_subjects.append({'source_table': table.name, 'source_row': n + 1, 'subject_table': subject_table,
                                               'subject_key': subject_key, 'decision': role,
                                               'matched_germplasmID': raw.get(G + 'germplasmID', '') if family == 'germplasm-score' and subject_table == 'material' else ''})
                    consumed_records(t, n, row, records, derived)
                    for name, record in records:
                        add_extension(name, record, t, n)
                        if name == 'material-identifier': material_identifiers[subject_key].add(record['identifier'])
                        if name == 'protocol' and record.get('protocolID'): trait_protocols[record['protocolID']].append(subject_key)
                elif role in {'bmde-context', 'nbn-context'}:
                    family = SUPPORTED_EXTENSIONS[table.row_type]; derived = derived_source(t, row)
                    # Absent optional registry columns carry no source value. Present columns must be approved together.
                    present = set(table.terms)
                    groups = GROUPS + [dict.fromkeys(UTM), dict.fromkeys(TIMES)] if family == 'bmde' else [dict.fromkeys(NBN_DATE)]
                    for group in groups:
                        if any(term in derived for term in group):
                            for term in group:
                                if term not in present: derived[term] = ''
                    if family == 'nbn' and any(term in derived for term in NBN_DATE):
                        if any(term in present and term not in derived for term in NBN_DATE):
                            raise ConversionError('NBN vague date columns must be approved or preserved together.', category='decision',
                                                  decision_ids=[item['id'] for item in columns_by_table[t] if item['term'] in NBN_DATE])
                        try:
                            date_value = nbn_event_date(*(derived.get(term, '') for term in NBN_DATE))
                            reason = 'Unsupported NBN vague date code; original endpoints are retained.' if date_value is None else None
                        except ImportFailure as error:
                            reason = str(error)
                        if reason:
                            for term in NBN_DATE:
                                value = derived.pop(term, '')
                                if value: withheld_values.append({'source_table': table.name, 'source_row': n + 1, 'term': term, 'value': value, 'reason': reason})
                    records = []
                    if family == 'bmde' and core.row_type == DWC + 'Occurrence':
                        occurrence_source = {term: value for term, value in derived.items() if any(term in group for group in GROUPS)}
                        records.extend(emit_legacy_records(family, occurrence_source, {}, 'occurrence', occurrence_keys[source_id], _key(archive, 'legacy', t, n)))
                        event_source = {term: value for term, value in derived.items() if term in UTM + TIMES}
                    else: event_source = derived
                    records.extend(emit_legacy_records(family, event_source, mapped, 'event', event_keys[source_id], _key(archive, 'legacy', t, n)))
                    consumed_records(t, n, row, records, derived)
                    for name, record in records: add_extension(name, record, t, n)
                elif role.startswith('media-'):
                    media = {field: value for field, value in mapped.get('media', {}).items() if value}
                    if not media:
                        raise ConversionError('A converted media row needs at least one mapped media value; preserve the extension instead of creating empty records.',
                                              category='conflict', decision_ids=[f'table:{t}', f'row:{t}:{n}'],
                                              evidence={'source_table': table.name, 'source_row': n + 1})
                    media_key = _key(archive, 'media', t, n)
                    media['media_pk'] = media_key
                    for target, primary, foreign in (('usage-policy', 'usagePolicy_pk', 'usagePolicy_fk'), ('provenance', 'provenance_pk', 'provenance_fk')):
                        description = {field: value for field, value in mapped.get(target, {}).items() if value}
                        if not description:
                            continue
                        signature = json.dumps(description, sort_keys=True, ensure_ascii=False)
                        key = _key(archive, target, signature)
                        media[foreign] = key
                        if signature not in media_groups[target]:
                            media_groups[target][signature] = key
                            add(target, {**description, primary: key}, t, n)
                        else:
                            trace(target, {primary: key}, t, n)
                    add('media', media, t, n)
                    subject = None
                    if role == 'media-occurrence':
                        subject = occurrence_keys[source_id]
                        add('occurrence-media', {'media_fk': media_key, 'occurrence_fk': subject}, t, n)
                    elif role == 'media-event':
                        subject = event_keys[source_id]
                        add('event-media', {'media_fk': media_key, 'event_fk': subject}, t, n)
                    media_subjects.append({'source_table': table.name, 'source_row': n + 1, 'media_pk': media_key,
                                           'decision': role, 'subject_key': subject, 'subject_basis': 'reviewed_core_attachment' if subject else 'explicitly_unlinked'})
                elif role == "molecular":
                    sequence = mapped.get("nucleotide-sequence", {}).get("sequence", "")
                    if not sequence:
                        raise ConversionError("Molecular conversion requires a mapped nonempty DNA sequence for each analysis.", category='conflict',
                                              decision_ids=[f'table:{t}'], evidence={'source_table': table.name, 'source_row': n + 1})
                    sequence_key = _key(archive, 'sequence', sequence)
                    protocol = mapped.get('molecular-protocol', {})
                    protocol_signature = json.dumps(protocol, sort_keys=True)
                    protocol_key = _key(archive, 'protocol', protocol_signature)
                    if sequence not in sequence_groups:
                        sequence_groups[sequence] = sequence_key
                        add('nucleotide-sequence', {'nucleotideSequence_pk': sequence_key, 'sequence': sequence}, t, n)
                    else:
                        trace('nucleotide-sequence', {'nucleotideSequence_pk': sequence_key}, t, n)
                    if protocol_signature not in protocol_groups:
                        protocol_groups[protocol_signature] = protocol_key
                        add('molecular-protocol', {**protocol, 'molecularProtocol_pk': protocol_key}, t, n)
                    else:
                        trace('molecular-protocol', {'molecularProtocol_pk': protocol_key}, t, n)
                    add("nucleotide-analysis", {"nucleotideAnalysis_pk": _key(archive, "analysis", t, n), "nucleotideSequence_fk": sequence_key,
                        "molecularProtocol_fk": protocol_key, "event_fk": event_keys[source_id]}, t, n)
            except ConversionError:
                raise
            except ImportFailure as error:
                # Emitter and raw-source failures for one extension row: retaining the table or row remedies them.
                raise ConversionError(f'{table.name}, row {n + 1}: {error}', category='conflict',
                                      decision_ids=[f'table:{t}', *([f'row:{t}:{n}'] if f'row:{t}:{n}' in issues_by_id else [])],
                                      evidence={'source_table': table.name, 'source_row': n + 1}) from error
    # Tables whose rows became occurrences, so individualCount was considered for every converted row.
    count_tables = [t for t, table in enumerate(archive.tables) if table.row_type == DWC + 'Occurrence'
                    and DWC + 'individualCount' in table.terms and (table.is_core or decisions.get(f'table:{t}') == 'occurrence')]
    for column in plan["columns"]:
        source_table = archive.tables[column["table"]]
        if column['term'] == DWC + 'individualCount' and column['table'] in count_tables:
            continue  # Replaced below with the row-level derived disposition.
        target = decisions.get(column["id"], column["default"])
        if not archive.tables[column["table"]].is_core and decisions.get(f"table:{column['table']}") == "preserve":
            target = "preserve"
        if target.startswith('material.') and decisions.get(f"material:{column['table']}", 'preserve') == 'preserve':
            target = 'preserve'
        if (column['table'], column['column']) in reviewed_value_columns:
            target_counts = {mapped_target: len(rows) for (t, c, mapped_target), rows in sorted(reviewed_target_rows.items())
                             if (t, c) == (column['table'], column['column']) and rows}
            mapped_count = sum(target_counts.values())
            dispositions.append({'source_table': source_table.name, 'term': column['term'],
                'target': 'reviewed value routes' if mapped_count else 'preserve',
                'target_counts': target_counts, 'disposition': 'mapped+retained' if mapped_count else 'retained-unmapped',
                'nonempty': column['nonempty'], 'mapped_rows': mapped_count,
                'retained_only_rows': column['nonempty'] - mapped_count})
            continue
        if target.startswith('occurrence-assertion.'):
            role = decisions.get(f"table:{column['table']}")
            if role == 'event-assertion': target = target.replace('occurrence-assertion.', 'event-assertion.')
            if role == 'declared-assertions': target = 'declared assertion subject → ' + target.split('.', 1)[1]
            if role and role.startswith('germplasm-score-'):
                target = role.rsplit('-', 1)[-1] + '-assertion.' + target.split('.', 1)[1]
        if target == PARENT_LINK:
            linked = len(column_consumption[(column['table'], column['term'])])
            extra = {'mapped_rows': linked, 'retained_only_rows': column['nonempty'] - linked}
            dispositions.append({"source_table": archive.tables[column["table"]].name, "term": column["term"],
                                 "target": 'derived parent event link → event.parentEvent_fk' if linked else 'preserve',
                                 "disposition": 'derived' if linked else 'retained-unmapped', "nonempty": column["nonempty"], **extra})
            continue
        family = SUPPORTED_EXTENSIONS.get(archive.tables[column['table']].row_type)
        # Event details on Occurrence extension rows are written only as the occurrence-events choice allows.
        extension_events = family == 'occurrence' and not archive.tables[column['table']].is_core and target.startswith('event.')
        special = family == 'humboldt' or family in GERMPLASM_FAMILIES.values() or family in LEGACY_FAMILIES.values() or extension_events
        if column['term'] in {NAME, QUALIFIER} and target == 'preserve' and column_consumption[(column['table'], column['term'])]:
            target = 'derived verbatim copy → ' + column.get('verbatim_copy', 'occurrence.verbatimIdentification')
            special = True
        if family == 'germplasm-score' and column['term'] == G + 'measurementTraitID' and column_consumption[(column['table'], column['term'])]:
            target = 'derived protocol link'
        extra = {}
        if special and target not in {'preserve', 'join'}:
            mapped_rows = len(column_consumption[(column['table'], column['term'])])
            extra = {'mapped_rows': mapped_rows, 'retained_only_rows': column['nonempty'] - mapped_rows}
            if not mapped_rows: target = 'preserve'
            elif target == 'derive': target = 'derived survey scope records' if family == 'humboldt' else 'derived extension records'
        skipped = skipped_by_table[column['table']]
        typed_skipped = typed_withheld[(column['table'], column['column'])] | placeholder_rows[(column['table'], column['column'])]
        if (skipped or typed_skipped) and not special and target not in {'preserve', 'join'}:
            copied = sum(bool(row[column['column']]) for n, row in enumerate(archive.tables[column['table']].rows) if n not in skipped and n not in typed_skipped)
            extra = {'mapped_rows': copied, 'retained_only_rows': column['nonempty'] - copied}
            if not copied: target = 'preserve'
        if placeholder_rows[(column['table'], column['column'])]:
            extra['empty_placeholder'] = len(placeholder_rows[(column['table'], column['column'])])
        if column['term'] == QUALIFIER and qualifier_retained[column['table']]:
            extra['retained_reasons'] = dict(sorted(qualifier_retained[column['table']].items()))
        dispositions.append({"source_table": archive.tables[column["table"]].name, "term": column["term"], "target": target,
                             "disposition": "retained-unmapped" if target == "preserve" else 'derived' if target == 'join' or target.startswith('derived ') else "mapped+retained", "nonempty": column["nonempty"], **extra})
    # Account for each converted occurrence's individualCount (core or extension)
    # as a value-level derived disposition: the individuals quantity pair, or an
    # individualCount assertion beside a different supplied quantity. Retained rows
    # have counts that are not nonnegative integers, or are extension rows kept in originals.
    output_rows = {(entry['source_row'], entry['source_table_index']): entry['target_row']
                   for entry in crosswalk if entry['target_table'] == 'occurrence'}
    assertion_rows = {(entry['source_row'], entry['source_table_index']): entry['target_row']
                      for entry in crosswalk if entry['target_table'] == 'occurrence-assertion'}
    for t in count_tables:
        table = archive.tables[t]
        count_term = DWC + 'individualCount'
        derived_values = []
        routes = Counter()
        invalid = empty = row_retained = 0
        for row_number, row in enumerate(table.rows, start=1):
            route, count = _individual_count(_source(table.terms, row))
            if route is None and count == 'empty':
                empty += 1
            elif (row_number, t) not in output_rows:
                row_retained += 1  # The whole extension row stays in the originals.
            elif route is None:
                invalid += 1
            else:
                routes[route] += 1
                if route != 'supplied' and len(derived_values) < DERIVED_VALUE_EXAMPLE_LIMIT:
                    derived_values.append({'source_row': row_number, 'source_value': count, **(
                        {'target_table': 'occurrence', 'target_row': output_rows[(row_number, t)],
                         'target_fields': {'organismQuantity': count, 'organismQuantityType': 'individuals'}}
                        if route == 'quantity' else
                        {'target_table': 'occurrence-assertion', 'target_row': assertion_rows.get((row_number, t)),
                         'target_fields': {'assertionType': 'individualCount', 'assertionValue': count, 'assertionUnit': 'individuals'}})})
        mapped = sum(routes.values())
        dispositions.append({
            'source_table': table.name, 'term': count_term,
            'target': 'derived individuals quantity → occurrence.organismQuantity, or individualCount assertion → occurrence-assertion.assertionValue',
            'target_counts': {target: routes[route] for route, target in (('quantity', 'occurrence.organismQuantity'),
                              ('assertion', 'occurrence-assertion.assertionValue')) if routes[route]},
            'disposition': 'derived' if mapped else 'retained-unmapped',
            'nonempty': mapped + invalid + row_retained,
            'mapped_rows': mapped,
            'retained_only_rows': invalid + row_retained,
            'empty_rows': empty,
            'derived_routes': {name: routes[route] for route, name in (('quantity', 'quantity_pair'), ('assertion', 'assertion'),
                               ('supplied', 'same_as_supplied_quantity')) if routes[route]},
            'derived_value_examples': derived_values,
            'derived_value_examples_omitted': routes['quantity'] + routes['assertion'] - len(derived_values),
            'mapping_rule': 'A nonnegative integer individualCount becomes organismQuantity with organismQuantityType=individuals when both '
                            'source quantity fields are empty. Beside a different supplied quantity, which keeps the quantity pair, it becomes '
                            'an occurrence-assertion with assertionType individualCount and assertionUnit individuals; a supplied quantity of '
                            'the same number of individuals already carries it. Zero does not determine occurrenceStatus; source '
                            'individualCount remains in the originals.',
            'retained_reasons': ({'invalid_nonnegative_integer': invalid,
                                  **({'row_retained_in_originals': row_retained} if row_retained else {})}
                                 if invalid or row_retained else {}),
        })
    survey_ids = defaultdict(set)
    for survey in resources.get('survey', []):
        if survey.get('surveyID'): survey_ids[survey['surveyID']].add(survey['survey_pk'])
    if any(len(keys) > 1 for keys in survey_ids.values()):
        raise ConversionError('A supplied surveyID would identify several separate surveys. Combine identical survey rows per event, or keep surveyID in the originals.',
                              category='conflict', decision_ids=[item['id'] for item in plan['columns'] if item['term'] == ECO_SURVEY_ID],
                              evidence={'surveyIDs': sorted(value for value, keys in survey_ids.items() if len(keys) > 1)[:5]})
    for group, material in material_groups.items():
        evidence = material_evidence[group]
        if len(evidence) == 1:
            material['evidenceForOccurrenceID'] = next(iter(evidence))
    # A source *ByID identifies an agent only when it is one absolute IRI. The
    # paired name is descriptive evidence, not an identity key: conflicting or
    # list-shaped names never get selected as a preferred name.
    origins = defaultdict(set)
    for entry in crosswalk:
        if entry['target_row'] is not None:
            origins[(entry['target_table'], entry['target_row'])].add(
                (entry['source_table_index'], entry['source_row'] - 1))
    agent_evidence = defaultdict(lambda: {'names': set(), 'origins': set()})
    skipped_agent_values = 0
    for resource_name, rows in list(resources.items()):
        descriptors = TABLE_SPECS[resource_name].field_descriptors
        for number, record in enumerate(rows, start=1):
            for field, identifier in record.items():
                if not field.endswith('ByID') or field[:-2] not in descriptors or not identifier or missing_reference(identifier):
                    continue
                name = record.get(field[:-2], '')
                if not _single_agent_iri(identifier):
                    # Role fields' ID lists are split by build_agent_roles below; others stay unlinked.
                    if (resource_name, field[:-2]) not in ROLE_FIELDS or not split_agent_ids(name, identifier, _single_agent_iri):
                        skipped_agent_values += 1
                    continue
                evidence = agent_evidence[identifier]
                if name and composite_name_reason(name) is None:
                    evidence['names'].add(agent_name(name))
                evidence['origins'].update(origins[(resource_name, number)])
    created_agents = 0
    for identifier, evidence in sorted(agent_evidence.items()):
        source_rows = sorted(evidence['origins'])
        if not source_rows:
            continue
        agent_key = _key(archive, 'agent', identifier)
        row = {'agent_pk': agent_key, 'agentID': identifier, 'preferredAgentName': ''}
        if len(evidence['names']) == 1:
            row['preferredAgentName'] = next(iter(evidence['names']))
        first_table, first_row = source_rows[0]
        add('agent', row, first_table, first_row)
        created_agents += 1
        for source_table, source_row in source_rows[1:]:
            trace('agent', {'agent_pk': agent_key}, source_table, source_row)
    unlinked_names = [issue['source_value'] for issue in [*plan['issues'], *plan.get('automatic_choices', [])]
                      if issue['id'].startswith('agent-share:') and decisions.get(issue['id']) == 'separate']
    agent_roles = build_agent_roles(resources, lambda *parts: _key(archive, *parts),
                                    link_names=decisions.get(AGENT_NAMES_ID, 'shared') == 'shared',
                                    unlinked_names=unlinked_names, is_agent_identifier=_single_agent_iri)
    for emitted in agent_roles.rows:
        source_rows = sorted({source for resource_name, number, _field in emitted.mentions
                              for source in origins[(resource_name, number)]})
        if not source_rows:
            raise ConversionError('An agent role has no traceable source row.', category='internal')
        source_table, source_row = source_rows[0]
        add(emitted.table, emitted.row, source_table, source_row)
        key_fields = TABLE_SPECS[emitted.table].primary_key or [field for field in emitted.row if field.endswith('_fk')]
        key = {field: emitted.row.get(field) for field in key_fields}
        for source_table, source_row in source_rows[1:]:
            trace(emitted.table, key, source_table, source_row, len(resources[emitted.table]))
    frames = {name: pd.DataFrame(rows).fillna("") for name, rows in resources.items()}
    validation = validate_dwc_dp_resources(frames)
    report = {"plan_id": plan["id"], "rule_version": RULE_VERSION, "source_sha256": namespace, "schema": plan["schema"],
              "decisions": user_decisions, 'effective_decisions': decisions,
              'automatic_choices': plan.get('automatic_choices', []),
              'warnings': _agent_names_warnings(plan.get('warnings', []), agent_roles.report),
              "columns": dispositions, "row_crosswalk": crosswalk, "files": plan["files"],
              'media_subjects': media_subjects,
              'extension_subjects': extension_subjects,
              'withheld_values': withheld_values, 'preserved_extension_rows': preserved_rows,
              'reviewed_value_routes': [{'decision_id': issue['id'], 'source_table': archive.tables[issue['table']].name,
                                         'source_value': issue['source_value'], 'count': issue['count'],
                                         'target': decisions.get(issue['id'], 'inactive: source column preserved')}
                                        for issue in [*plan['issues'], *plan.get('automatic_choices', [])]
                                        if issue['id'].startswith(('country-label:', 'age-remark:'))],
              'agent_mapping': {'created': created_agents,
                                'without_preferred_name': sum(bool(value['origins']) and len(value['names']) != 1
                                                              for value in agent_evidence.values()),
                                'non_single_id_cells': skipped_agent_values},
              'agent_roles': agent_roles.report,
              **({'event_hierarchy': hierarchy_report} if hierarchy_report else {}),
              **({'depth_events': {'combined_events': len(depth_children), 'depth_events': sum(map(len, depth_children.values())),
                                   'fields': [field for field in DEPTH_FIELDS if any(field in event for event in resources['event'])],
                                   'policy': 'Occurrences sharing an eventID form one event. Each distinct supplied depth becomes a child event '
                                             'inside it that holds only the depth values, its eventCategory and its parent link; its occurrences '
                                             'link to it. Combined events carry no depth range, and no child eventID is created.'}}
                 if depth_split else {}),
              **({'shared_event_ids': {'parent_events': len(shared_children), 'row_events': sum(shared_children.values()),
                                       'policy': 'Each occurrence row has its own event. Rows that share a supplied eventID link to one '
                                                 'parent event that holds only that eventID and an eventCategory. Placeholder eventIDs '
                                                 'such as NA stay only in the originals, so no eventID repeats. '
                                                 'Row events keep their own details; nothing is combined or inherited.'}}
                 if shared_children else {}),
              "resources": {name: len(df) for name, df in frames.items()}, "validation": validation,
              "limitations": ["Structural validation does not prove semantic equivalence.", "Original files retain unsupported columns and extensions.",
                              "Internal keys identify converted rows; source identifiers are retained separately.",
                              "Material records require review; organism and sampling relationships are not inferred.",
                              "Multiple occurrences sharing material remain linked in the row crosswalk; this snapshot permits only one evidenceForOccurrenceID per material row."]}
    return frames, report
