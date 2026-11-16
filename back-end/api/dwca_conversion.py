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
from api.dwca_tidy import column_note, settled_values
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
from api import dwca_glossary
from api.dwca_preflight import SEQUENCE, preflight
from api.dwca_review import (apply_policy, effective_decisions, failed_requirements, group_rows,  # noqa: F401
                             option_status, remove_unavailable, violations, conditional_defaults, asked)
from api.dwca_semantic_audit import ASSERTION_IRI_FIELDS, is_age_like_remark, semantic_target_rejection
from api.dwca_agents import ROLE_FIELDS, agent_name, build_agent_roles, composite_name_reason, split_agent_ids

RULE_VERSION = "30"
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
# Core sampling columns describe the row's event. The event table has no field for them, so they
# become protocol records linked by event-protocol, and a paired sample size goes to a survey record
# on survey events (otherwise an event assertion). See docs/dwca-conversion/README.md, rule version 29.
SAMPLING_PROTOCOL = DWC + 'samplingProtocol'
SAMPLE_SIZE = (DWC + 'sampleSizeValue', DWC + 'sampleSizeUnit')
SAMPLING_TERMS = {SAMPLING_PROTOCOL, *SAMPLE_SIZE}
SAMPLING_CHOICES = {
    SAMPLING_PROTOCOL: ('Method descriptions linked to each event',
                        'Each sampling protocol becomes a method description linked to its event; events with the same protocol share '
                        'one, and a " | " list becomes one per protocol. On survey events the text is also kept on the survey description.'),
    **{term: ('Sample size of each event',
              'The sample size and its unit are kept together: on the survey description of a survey event, otherwise as a '
              'measurement of the event. A value without its unit, or a unit without its value, is left out of the Data Package.')
       for term in SAMPLE_SIZE},
}
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
COUNT_REASON = ('individualCount is not copied as a column of its own. Each whole-number count becomes the number of individuals '
                'of the occurrence (its organism quantity); where the row already gives a different quantity, the count is kept as a '
                'measurement of the occurrence instead. Other values stay only in your original files.')
# What an unfamiliar additional table holds, said before its "what do these rows describe?" question.
TABLE_REASONS = {
    'germplasm-accession': 'This table holds gene bank passport details, such as accession numbers, pedigrees and donors.',
    'germplasm-score': 'This table holds measured or scored trait values, one value per row.',
    'germplasm-trait': ('This table defines the traits that were measured: their names, methods and sources. ChatIPT can keep each row '
                        'as a method description for the whole dataset, not attached to any particular record.'),
    'germplasm-trial': 'This table describes the field trial each event belongs to, such as its year, location and report.',
}
# The pinned DwC-DP 1.0_DEV requires nucleotide-analysis.nucleotideSequence_fk (final DwC-DP 1.0 does not, tdwg/dwc#934).
# A row without a sequence, typically a targeted qPCR/ddPCR result, therefore stays in the originals; no stand-in sequence is made.
SEQUENCELESS_DNA = ('This version of the Data Package format can record a DNA analysis only together with the sequence it produced, '
                    'so {what} left out of the Data Package. No sequence is made up.')
SEQUENCELESS_DNA_ROW = 'This DNA row has no DNA sequence. ' + SEQUENCELESS_DNA.format(
    what='a targeted test result, such as a qPCR or ddPCR detection or Cq value, is')
NAMESPACE = uuid.UUID("7750ccce-e9f9-4fd1-b9d8-a02a9747cae9")
PRESERVE = {"value": "preserve", "label": dwca_glossary.PRESERVE['label']}
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


def _target_words(target):
    """A mapping target in words: 'event.eventDate' -> 'event date on the event record'."""
    table, field = target.split('.', 1)
    record = dwca_glossary.TABLES.get(table, {}).get('label', table.replace('-', ' '))
    return f'{dwca_glossary.field_label(field)} on the {record}'


def short_term(term):
    """A term's name as people know it from their column headers: '.../germplasmTerm#measurementTraitName' -> 'measurementTraitName'."""
    return re.split(r'[/#]', term)[-1]


def _column_id(t, c):
    return f"column:{t}:{c}"


def _reviewed_value_id(kind, t, c, value):
    digest = hashlib.sha256(value.encode('utf-8')).hexdigest()
    return f'{kind}:{t}:{c}:{digest}'


AGENT_NAMES_ID = 'agent-names'
READ_COUNTS_ID = 'dna-read-counts'


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
                f"{names:,} {'name' if names == 1 else 'names'} without an identifier "
                f"{'was' if names == 1 else 'were each'} listed once as a person or organization and linked to "
                f"{linked:,} {'mention' if linked == 1 else 'mentions'}"
                + (f"; {kept:,} {'mention was' if kept == 1 else 'mentions were'} kept as text only, as you chose" if kept else '')
                + '. Names with an identifier are linked by that identifier.')}
        restated.append(warning)
    return restated


def _agent_name_id(value):
    return 'agent-share:' + hashlib.sha256(value.encode('utf-8')).hexdigest()


def _protocol_field(item):
    """A listed sampling protocol that is only a web link is a reference; anything else names the protocol."""
    return 'protocolReferences' if re.fullmatch(r'https?://\S+', item.strip()) else 'protocolName'


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
        return {"value": target, "label": "Link each row to the event with this eventID"}
    if target == 'derive':
        return {"value": target, "label": 'Convert as described above'}
    explanation = dwca_glossary.explain(target)
    if explanation is None or 'technical' not in explanation:
        return {"value": target, "label": target.replace(".", " → ")}
    return {"value": target, "label": explanation['label'], 'technical': explanation['technical']}


def _glossary(plan):
    """Plain-language glossary entries that are relevant to the choices in this plan."""
    targets = set()
    for item in [*plan.get('issues', []), *plan.get('automatic_choices', [])]:
        for option in [*item.get('options', []), *item.get('unavailable_options', [])]:
            value = option.get('value', '')
            if '.' in value:
                targets.add(value)
    for column in plan.get('columns', []):
        if column.get('follows') or column.get('default_when'):
            values = [column.get('default')]
            values.extend(branch.get('value') for branch in column.get('default_when', []))
            targets.update(value for value in values if isinstance(value, str) and '.' in value)

    found, table_names = {}, set()
    for target in sorted(targets):
        table, field = target.split('.', 1)
        spec = TABLE_SPECS.get(table)
        if spec is None or field not in spec.field_descriptors:
            continue
        explanation = dwca_glossary.explain(target) or {}
        description = spec.field_descriptors[field].get('description') or ''
        definition = description if len(description) <= 300 else description[:299] + '…'
        found[target] = {**explanation, 'decided': dwca_glossary.decided(target),
                         'field_label': dwca_glossary.field_label(field), 'definition': definition}
        table_names.add(table)
    if any(issue.get('id', '').startswith('material:')
           for issue in [*plan.get('issues', []), *plan.get('automatic_choices', [])]):
        table_names.add('material')
    return {
        'revision': dwca_glossary.SCHEMA_REVISION,
        'copy_note': dwca_glossary.COPY_NOTE,
        'preserve': dwca_glossary.PRESERVE,
        'families': {name: family['summary'] for name, family in sorted(dwca_glossary.FAMILIES.items())},
        'specimen_details': {
            'stored': dwca_glossary.SPECIMEN_DETAILS_STORED,
            'stored_why': dwca_glossary.SPECIMEN_DETAILS_STORED_WHY,
            'kept': dwca_glossary.SPECIMEN_DETAILS_KEPT,
            'kept_why': dwca_glossary.SPECIMEN_DETAILS_KEPT_WHY,
            'pending': dwca_glossary.SPECIMEN_DETAILS_PENDING,
            'pending_why': dwca_glossary.SPECIMEN_DETAILS_PENDING_WHY,
        },
        'tables': {name: dwca_glossary.TABLES[name] for name in sorted(table_names) if name in dwca_glossary.TABLES},
        'targets': found,
    }


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
        return 'Not a valid value for this field, so it was not copied. Your original files keep it unchanged.'
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
                raise ImportFailure(f'Two columns in one table both stand for {short_term(term)} but hold different values, '
                                    'so ChatIPT cannot tell which one to use. Remove or rename one of them.')
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


def _read_count(quantity, quantity_type):
    """organismQuantity as a whole number of DNA sequence reads (readCount), or None."""
    quantity = quantity.strip()
    if quantity_type.strip().casefold() != 'dna sequence reads' or not re.fullmatch(r'\+?\d+', quantity):
        return None
    return str(int(quantity))


def _dna_rows(archive):
    """{core id: DNA Derived Data rows linked to it across all DNA tables}, converted or not.

    An occurrence's read count belongs to its analysis only when it has exactly one: with several
    (markers, say), the count cannot be split, and preserving one row must not move it to another.
    """
    return Counter(source_id for table in archive.tables if not table.is_core
                   and SUPPORTED_EXTENSIONS.get(table.row_type) == 'molecular' for source_id in table.ids)


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
    author_tokens = author.split(' ')
    filius = (len(author_tokens) >= 2 and author_tokens[-1] in {'f.', 'fil.'}
              and author_tokens[-2].endswith('.')
              and (author_tokens[-2][0].isupper() or author_tokens[-2].startswith('(')))
    rank_tokens = author_tokens[:-1] if filius else author_tokens
    if (any(token in AUTHOR_PARTICLES | NOT_EPITHETS for token in words[1:])
            or any(token.casefold() in HYBRID_MARKERS for token in tokens) or '×' in name
            # 'Aus sp. A' is an informal designation; 'Aus bus L. subsp. cus' has an infraspecific name after an author.
            or (author and (words[-1] in {'sp.', 'spp.', 'sp', 'spp'} or not (author[0] in '([' or author[0].isupper())
                            or any(token in RANK_MARKERS for token in rank_tokens)))):
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
        # Archive row keys are never used as parents.
        return None, ("The main table has no eventID column, so the events named in parentEventID cannot be found. "
                      "The column is left out of the Data Package.")
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
                    'reason': 'The rows combined into this event disagree on this detail, so it could not be compared.',
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
                check['reason'] = 'The rows combined into one of these events give different locations, so the locations could not be compared.'
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


def _example_values(values):
    counts = Counter(value for value in values if value)
    first = {value: position for position, value in enumerate(counts)}
    ordered = sorted(counts, key=lambda value: (-counts[value], first[value]))[:3]
    return ' · '.join(value[:40].rstrip() + ('…' if len(value) > 40 else '') for value in ordered)


def _material_modes(table, material_column, comparison_term=None, recorded=False):
    """Material modes ('per_row', 'by_id') under which a column can default to its material field.

    by_id needs a usable material identifier on every row, and the column's values (comparison_term)
    must agree within each identifier, or combining would conflict. With recorded=True the column
    names collectors, which GBIF reads from a specimen only through one unambiguous link: each
    material must name exactly one occurrenceID, used by no other material (as convert() sets
    evidenceForOccurrenceID).
    """
    occurrence_col = table.terms.index(DWC + 'occurrenceID') if DWC + 'occurrenceID' in table.terms else None
    occurrence_ids = [row[occurrence_col] for row in table.rows] if occurrence_col is not None else []
    linked = bool(occurrence_ids) and all(value and not missing_reference(value, ()) for value in occurrence_ids)
    # A repeated occurrenceID names several converted occurrences, so no specimen links to just one.
    if recorded and (not linked or len(set(occurrence_ids)) != len(occurrence_ids)):
        return []
    modes = ['per_row']
    if material_column is None:
        return modes
    groups = defaultdict(list)
    for row in table.rows:
        identifier = row[material_column]
        if not identifier or missing_reference(identifier, ()):
            return modes
        groups[identifier].append(row)
    if comparison_term is not None:
        column = table.terms.index(comparison_term)
        if any(len({row[column] for row in rows}) > 1 for rows in groups.values()):
            return modes
    if recorded:
        per_group = [{row[occurrence_col] for row in rows} for rows in groups.values()]
        if any(len(ids) != 1 for ids in per_group) or len({next(iter(ids)) for ids in per_group}) != len(per_group):
            return modes
    return [*modes, 'by_id']


def _material_identifier_column(table):
    """The one column that can identify combined material, or None.

    When both materialSampleID and materialEntityID have values, either may become the identifier
    by_id combines on, so no single column predicts which rows share a material.
    """
    supplied = [table.terms.index(term) for term in (DWC + 'materialSampleID', DWC + 'materialEntityID')
                if term in table.terms and any(row[table.terms.index(term)] for row in table.rows)]
    return supplied[0] if len(supplied) == 1 else None


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
        'No count is zero and no row says "absent" or "not found", so occurrenceStatus is set to present, which is how GBIF '
        'reads these records anyway. Change this to absent if the records report organisms that were looked for but not found.')}


def _repeated_traits(table):
    """A note when a trait table repeats its definitions, since each row becomes its own method description."""
    if SUPPORTED_EXTENSIONS.get(table.row_type) != 'germplasm-trait':
        return ''
    # A column that only repeats each row's link to the main table does not make a definition different.
    links = {c for c in range(len(table.terms)) if table.ids and all(row[c] == key for row, key in zip(table.rows, table.ids))}
    distinct = len({tuple(value for c, value in enumerate(row) if c not in links) for row in table.rows})
    if distinct == len(table.rows):
        return ''
    per_record = ', one row for each record it is attached to,' if len(set(table.ids)) == len(table.rows) else ''
    return (f'Here the table has {len(table.rows):,} rows{per_record} but only {distinct:,} different trait '
            f'{"definition" if distinct == 1 else "definitions"}. Each row becomes its own method description, so the '
            'repeated ones would appear many times. ')


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
                reason = ('Some events these survey rows describe have an eventCategory other than survey, and ChatIPT does not '
                          'overwrite a category from your files. The survey table is left out of the Data Package. To convert it, '
                          'correct eventCategory in your files and upload them again.')
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
            reason = 'This whole table is left out of the Data Package, because ChatIPT cannot convert what its rows describe.'
        elif {option['value'] for option in issue['options']} == {'preserve'}:
            default = 'preserve'
            # When every other option was ruled out by the data, saying why is the explanation.
            ruled_out = ' '.join(dict.fromkeys(option['reason'] for option in issue.get('unavailable_options', []) if option.get('reason')))
            reason = (ruled_out or issue['reason']) if not issue['id'].startswith('table:') else (
                plan['tables'][issue['table']].get('conversion_unavailable') or
                archive.tables[issue['table']].unplaced_reason or
                f'ChatIPT cannot yet convert this kind of table when it is attached to {core.name}, so it is left out of the Data Package.')
        elif issue['id'] == 'event-grain' and (event_ids is None or any(not row[event_ids] or
                missing_reference(row[event_ids], ()) for row in core.rows)):
            default = 'per_row'
            reason = 'Some occurrences have no usable eventID, so each occurrence row gets its own event and no events are combined.'
            issue = {**issue, 'options': [option for option in issue['options'] if option['value'] == 'per_row']}
            supplied = [row[event_ids] for row in core.rows if row[event_ids] and
                        not missing_reference(row[event_ids], ())] if event_ids is not None else []
            if len(set(supplied)) != len(supplied):
                # Splitting a repeated persistent identity is an interpretation,
                # even when missing IDs make complete grouping unavailable.
                required.append({**issue, 'reason': reason + ' Some eventIDs are shared by several rows. Confirm that each row gets its own event, '
                                 'grouped inside one event for each shared eventID, or leave eventID out of the Data Package in the mapping choices.'})
                continue
        elif issue['id'] == 'event-grain' and len({row[event_ids] for row in core.rows}) == len(core.rows):
            default = 'by_id'
            reason = 'Every occurrence has a different eventID, so each row becomes its own event and nothing is combined.'
        elif issue.get('convention'):
            # A default GBIF infers anyway: applied visibly and changeable, never asked.
            default, reason = issue['convention']['default'], issue['convention']['reason']
        elif issue['id'].startswith('material:') and issue.get('strong_specimen_signal'):
            default = 'per_row'
            # No persistent material identifier is inferred.
            reason = ('Every row is a PreservedSpecimen with its own complete institutionCode, collectionCode and '
                      'catalogNumber, so each row becomes one specimen record.')
            if issue.get('followers') and ' If yes,' in issue['reason']:
                reason += ' If yes,' + issue['reason'].split(' If yes,', 1)[1]
        elif issue['id'] == AGENT_NAMES_ID:
            default = 'shared'
            # Other choices (a column kept in the originals, a name kept as text) can lower these counts;
            # the conversion report restates the notice with what was linked.
            reason = (f"Up to {issue['count']:,} {'mention' if issue['count'] == 1 else 'mentions'} of "
                      f"{issue['names']:,} {'name' if issue['names'] == 1 else 'names'} {'has' if issue['count'] == 1 else 'have'} no identifier. Each exact name is "
                      "listed once as a person or organization and linked to all its mentions. Choose 'Keep all names without "
                      "identifiers as text only' if the same name may stand for different people or organizations. Names with an "
                      "identifier are linked by that identifier.")
        elif issue['id'] == READ_COUNTS_ID:
            default = 'copy'
            # A converted DNA table, sequence and quantity columns are needed too; the report gives what was copied.
            reason = (f"Up to {issue['count']:,} {'occurrence gives its' if issue['count'] == 1 else 'occurrences give their'} organismQuantity "
                      f"in DNA sequence reads and {'has' if issue['count'] == 1 else 'have'} one DNA row each, so each count is also recorded "
                      "as the read count of that DNA analysis. The occurrence keeps it too. Occurrences with several DNA rows keep "
                      "their count only on the occurrence, because it cannot be split between analyses. Choose 'Keep read counts only "
                      "on the occurrences' to leave the DNA analyses without one.")
        elif issue['id'].startswith('agent-share:'):
            default = 'shared'
            reason = (f"The {issue['count']:,} mentions of {issue['source_value'][:100]!r} are linked to one person or "
                      "organization. Choose 'Keep this name as text only' if they are different people or organizations.")
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
                reason = 'Every event these rows describe already has eventCategory survey in your files.'
            elif category_choice and all(categories.get(source_id, '') in {'', 'survey'} for source_id in table.ids):
                default = 'require'
                reason = ('Event categories come from your files or from your answer about events with no category. Survey '
                          'descriptions are converted only for survey events; this step never fills in or changes a category.')
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
                or issue.get('convention') or issue['id'] in {AGENT_NAMES_ID, READ_COUNTS_ID}):
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
    issues, columns, profiles, warnings, row_issues, proposed_automatic = [], [], [], dropped_extension_warnings(archive), [], []
    nodes, hierarchy_unsupported, hierarchy = _hierarchy(archive, core)
    scientific = _scientific_hierarchy(archive, core, nodes, hierarchy)
    if not archive.has_meta:
        issues.append(_issue("loose-links", "Check how your files fit together",
            "There is no meta.xml, so ChatIPT worked out what each file is from its name, and links rows between files by their shared identifiers (never by row order). Check the files and links under Conversion details, then confirm.",
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
                options.insert(0, {"value": "identification", "label": "Identifications of the linked occurrences"}) if core.row_type == DWC + "Occurrence" else None
            elif family == "assertion":
                explicit_occurrences = DWC + 'occurrenceID' in table.terms and any(row[table.terms.index(DWC + 'occurrenceID')] for row in table.rows)
                if core.row_type == DWC + 'Event' and explicit_occurrences:
                    options.insert(0, {'value': 'event-assertion', 'label': 'Measurements or facts about the linked events'})
                    options.insert(0, {'value': 'declared-assertions', 'label': 'Measurements or facts about the occurrence named in occurrenceID, otherwise about the linked event'})
                else:
                    options.insert(0, {"value": "event-assertion", "label": "Measurements or facts about the linked events"})
                if core.row_type == DWC + "Occurrence":
                    options.insert(0, {"value": "occurrence-assertion", "label": "Measurements or facts about the linked occurrences"})
            elif family == "relationship":
                options.insert(0, {"value": "resource-relationship", "label": "Relationships between the records they name, keeping their identifiers"})
            elif family == "molecular":
                options.insert(0, {"value": "molecular", "label": "DNA sequences, with their methods and analyses, linked to the event"})
            elif family in {'media', 'eol-media'}:
                options.insert(0, {'value': 'media-unlinked', 'label': 'Media files not linked to any record'})
                options.insert(0, {'value': 'media-event', 'label': 'Each media file shows the linked event'})
                if core.row_type == DWC + 'Occurrence':
                    options.insert(0, {'value': 'media-occurrence', 'label': 'Each media file shows the linked occurrence'})
            elif family in {'identifier', 'reference', 'eol-reference'}:
                options.insert(0, {'value': family, 'label': ('Other identifiers of the linked record' if family == 'identifier' else 'Literature references for the linked record')})
            elif family == 'humboldt':
                if core.row_type == DWC + 'Event':
                    options[:0] = [{'value': 'humboldt-survey', 'label': 'Each row describes a separate survey of its event'},
                                   {'value': 'humboldt-merge', 'label': 'One survey per event (only if all rows for an event are identical)'}]
                elif DWC + 'eventID' in core.terms and all(
                        row[core.terms.index(DWC + 'eventID')] and
                        not missing_reference(row[core.terms.index(DWC + 'eventID')], ()) for row in core.rows):
                    options.insert(0, {'value': 'humboldt-grouped', 'label': 'One survey for each shared eventID (only if every occurrence with that eventID has an identical row)'})
            elif family == 'germplasm-accession' and material_context:
                options.insert(0, {'value': 'germplasm-accession', 'label': 'Accession numbers and passport details of the specimen records'})
            elif family == 'germplasm-score':
                options.insert(0, {'value': 'germplasm-score-event', 'label': 'Measurements of the linked event'})
                if core.row_type == DWC + 'Occurrence':
                    options.insert(0, {'value': 'germplasm-score-occurrence', 'label': 'Measurements of the linked occurrence'})
                if material_context:
                    options.insert(0, {'value': 'germplasm-score-material', 'label': "Measurements of the specimen records (each row's germplasmID must match its specimen's accession number)"})
            elif family == 'germplasm-trait':
                options.insert(0, {'value': 'germplasm-trait', 'label': 'Trait definitions, kept as method descriptions for the whole dataset'})
            elif family == 'germplasm-trial' and core.row_type == DWC + 'Event':
                options.insert(0, {'value': 'germplasm-trial', 'label': 'Trial details added to the linked event, if they do not contradict it'})
            elif family in {'bmde', 'nbn'}:
                options.insert(0, {'value': family + '-context', 'label': ('Measurements of the occurrence, with event details added to its event' if family == 'bmde' and core.row_type == DWC + 'Occurrence' else 'Details of the linked event')})
            media_signals = [term for term in table.terms if term in MEDIA_SUBJECT_TERMS and any(row[table.terms.index(term)] for row in table.rows)] if family == 'media' else []
            issues.append(_issue(f"table:{t}", f"{table.name}: what do these rows describe?",
                table.unplaced_reason or ('Being attached to a record does not say what a picture or recording shows. Choose whether each media file shows the linked occurrence or its event, keep the media without a link, or leave it out of the Data Package.' if family == 'media' else
                    (TABLE_REASONS.get(family, '') + ' ' if family in TABLE_REASONS else '') + _repeated_traits(table) + f"Choose what the rows of {table.name} describe and how they link to {core.name}, or leave the table out of the Data Package."), options,
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
                ('Every row is a PreservedSpecimen with its own institutionCode, collectionCode and catalogNumber. '
                 if strong_specimen else 'The rows are preserved specimens with catalogue details, but some catalogue numbers repeat or are incomplete. '
                 if specimen_context else 'Some rows have specimen or sample identifiers. ') +
                'If these identify physical things, such as a specimen, tissue or soil sample, they can become specimen records: '
                'one per row, or one per identifier when all of its rows agree. Otherwise the identifiers are left out of the Data Package.',
                [{**PRESERVE, 'label': 'No: leave the identifiers out of the Data Package'}, {'value': 'per_row', 'label': 'Yes: one specimen record per row'},
                 {'value': 'by_id', 'label': 'Yes: one specimen record per identifier'}],
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
                if term in {'http://purl.org/dc/terms/format', 'http://purl.org/dc/terms/created',
                            'http://purl.org/dc/terms/creator', 'http://purl.org/dc/elements/1.1/type'} and len(options) == 1:
                    media_reason = None
            if family in {'eol-media', 'eol-reference'}:
                options, media_reason = eol_targets(table.row_type, term, values)
                if family == 'eol-reference' and options and any(source.get('http://eol.org/schema/reference/full_reference') for source in sources) and term != 'http://eol.org/schema/reference/full_reference':
                    media_reason = (media_reason or '') + ' Your rows also have full citations, and EOL ignores the separate citation parts when a full citation is given. Confirm this column should be copied too, or leave it out of the Data Package.'
            if family == 'humboldt':
                options, media_reason = humboldt_targets(term, sources, humboldt_blocked)
                if term in DIRECT or term in IRI_DIRECT:
                    # Validation can withhold incompatible cells without guessing
                    # their meaning. A strict agent IRI keeps its identifier role.
                    if media_reason:
                        warnings.append({'id': _column_id(t, c), 'title': short_term(term),
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
                    options = []; media_reason = 'BMDE measurements describe an occurrence, but this table is attached to events, so ChatIPT cannot tell which occurrence each one belongs to.'
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
            occurrence_context = own == 'occurrence' and (table.is_core or family == 'occurrence')
            default_when = []
            follows_names = None
            automatic_family = None
            automatic_reason = None
            glance = True
            special_chosen = None
            sampling_label = None
            if table.is_core and term in SAMPLING_TERMS:
                options, media_reason = ['derive'], None
                sampling_label, automatic_reason = SAMPLING_CHOICES[term]
                automatic_family = 'events'
            if family == 'media' and term in {'http://purl.org/dc/terms/format', 'http://purl.org/dc/terms/created',
                                              'http://purl.org/dc/terms/creator', 'http://purl.org/dc/elements/1.1/type'} and len(options) == 1:
                target = options[0]
                automatic_family, automatic_reason = 'media', (
                    f'Values in {short_term(term)} are {dwca_glossary.decided(target)}, exactly as written. This is the standard match between the media vocabulary and the Data Package; choose "{PRESERVE['label']}" if the column means something else.')
            if term == NAME and occurrence_context:
                options = [target for target in options if target.startswith('occurrence.')]
            if term == DWC + 'recordedBy' and has_material and occurrence_context and 'material.collectedBy' in options:
                n, d = len(values), len(set(values))
                ex = _example_values(values)
                occurrence_col = table.terms.index(DWC + 'occurrenceID') if DWC + 'occurrenceID' in table.terms else None
                identifier_col = _material_identifier_column(table)
                modes = _material_modes(table, identifier_col, term, recorded=True)
                link_condition = ([{'type': 'target_in', 'column': _column_id(t, occurrence_col),
                                    'targets': ['occurrence.occurrenceID']}] if occurrence_col is not None else [])
                basis_values = [row[table.terms.index(DWC + 'basisOfRecord')] for row in table.rows
                                if DWC + 'basisOfRecord' in table.terms and row[table.terms.index(DWC + 'basisOfRecord')]]
                basis = ('your rows are specimens or samples (' + ' / '.join(list(dict.fromkeys(basis_values))[:4]) + ') and '
                         if basis_values else '')
                record_index = table.terms.index(DWC + 'recordNumber') if DWC + 'recordNumber' in table.terms else None
                rn = (f" The collector numbers (recordNumber, {sum(bool(row[record_index]) for row in table.rows):,} rows) are stored on the same specimen records."
                      if record_index is not None and any(row[record_index] for row in table.rows) and
                      'material.collectorNumber' in _candidates(DWC + 'recordNumber', target_tables) else '')
                for mode in modes:
                    created = ('a specimen record is created for each row' if mode == 'per_row' else
                               'rows that share a specimen identifier become one specimen record, linked to one observation')
                    default_when.append({'value': 'material.collectedBy',
                        'when': [{'type': 'decision_in', 'id': f'material:{t}', 'values': [mode]}, *link_condition],
                        'reason': f"{n:,} rows name people in recordedBy ({d:,} different names, e.g. {ex}). They are saved as who collected the specimen, on each specimen record. "
                                  f"Why: {basis}{created}, and in specimen collections this column names the collector.{rn}"})
                # GBIF reads collectors from a specimen only through one unambiguous link to its observation.
                occurrence_ids = [row[occurrence_col] for row in table.rows] if occurrence_col is not None else None
                if occurrence_ids is None:
                    why = "no occurrenceID column links specimen records to their observations"
                elif not all(value and not missing_reference(value, ()) for value in occurrence_ids):
                    why = "some rows have no usable occurrenceID, so a specimen record could not be linked to its observation"
                elif len(set(occurrence_ids)) != len(occurrence_ids):
                    why = "some occurrenceIDs repeat, so a specimen record could not be linked to just one observation"
                else:
                    why = "when specimen records are combined by identifier, some would not link to exactly one observation"
                ambiguous_reason = (f"{n:,} rows name people in recordedBy ({d:,} different names, e.g. {ex}). They are saved as who saw or recorded the organism, on each observation record. "
                                    f"Why: {why}, and GBIF reads collectors from the observation record in that case.")
                default_when.append({'value': 'occurrence.recordedBy',
                    'when': [{'type': 'decision_in', 'id': f'material:{t}', 'values': ['per_row', 'by_id']}],
                    'reason': ambiguous_reason})
                fallback_reason = (f"{n:,} rows name people in recordedBy ({d:,} different names, e.g. {ex}). They are saved as who saw or recorded the organism, on each observation record. Why: no specimen records are created for these rows.")
                automatic_family, automatic_reason = 'agent-role', fallback_reason
                # The usual occurrence target remains the plain fallback.
                special_chosen = 'occurrence.recordedBy'
            if term == DWC + 'recordedByID' and occurrence_context:
                recorded_col = table.terms.index(DWC + 'recordedBy') if DWC + 'recordedBy' in table.terms else None
                if recorded_col is not None:
                    name_options = _candidates(DWC + 'recordedBy', target_tables)
                    if has_material and 'material.collectedBy' in name_options:
                        name_options = ['occurrence.recordedBy', *[target for target in name_options if target != 'occurrence.recordedBy']]
                    partners = {}
                    for (resource, name_field), role in ROLE_FIELDS.items():
                        partners[f'{resource}.{name_field}'] = f'{resource}.{role.id_field}'
                    options = list(dict.fromkeys(partners[target] for target in name_options if target in partners))
                    default_when = []
                    for target in name_options:
                        if target not in partners or target == 'occurrence.recordedBy':
                            continue
                        record_kind = {'material': 'specimen', 'event': 'event', 'occurrence': 'observation'}[target.split('.')[0]]
                        default_when.append({'value': partners[target],
                            'when': [{'type': 'target_in', 'column': _column_id(t, recorded_col), 'targets': [target]}],
                            'reason': f"Identifiers in recordedByID follow the names in recordedBy, so each name keeps its identifier: on each {record_kind} record."})
                    special_chosen = 'occurrence.recordedByID'
                    follows_names = {'column': _column_id(t, recorded_col),
                                     'targets': {partners[target]: target for target in name_options if target in partners}}
                    automatic_family, automatic_reason, glance = 'agent-role', 'Identifiers in recordedByID follow the names in recordedBy, so each name keeps its identifier: on each observation record.', False
                else:
                    options = ['occurrence.recordedByID']
                    special_chosen = 'occurrence.recordedByID'
            if term in {DWC + 'typeStatus', DWC + 'typeDesignationType'} and occurrence_context:
                field = term.rsplit('/', 1)[-1]
                special_chosen = f'identification.{field}'
                if has_material:
                    material_target = f'material.{field}'
                    identifier_col = _material_identifier_column(table)
                    modes = _material_modes(table, identifier_col, term)
                    if material_target in options:
                        if modes:
                            ex = _example_values(values)
                            short = term.rsplit('/', 1)[-1]
                            description = 'type status' if short == 'typeStatus' else short
                            disagreement = 'different type statuses' if short == 'typeStatus' else f'different {short} values'
                            default_when = [
                                *({'value': material_target, 'when': [{'type': 'decision_in', 'id': f'material:{t}', 'values': [mode]}],
                                   'reason': f"{len(values):,} rows give a {description} (e.g. {ex}). It is saved as the {description} of each specimen record, because "
                                             + ('a specimen record is created for each row' if mode == 'per_row' else
                                                'rows that share a specimen identifier become one specimen record, and they agree on it')
                                             + ', and a type status describes a specimen.'} for mode in modes),
                                {'value': special_chosen, 'when': [{'type': 'decision_in', 'id': f'material:{t}', 'values': ['per_row', 'by_id']}],
                                 'reason': f"{len(values):,} rows give a {description} (e.g. {ex}). It is saved with the identification of each observation, because combined specimen records would mix {disagreement}."}]
                automatic_family = 'type-status'
                description = 'type status' if field == 'typeStatus' else field
                automatic_reason = f"{len(values):,} rows give a {description} (e.g. {_example_values(values)}). It is saved with the identification of each observation, because no specimen records are created for these rows."
            parent_blocked = parent_default = None
            if term == PARENT and (table.is_core or family == 'occurrence'):
                # Parent links are resolved from persistent eventIDs of source Event identities, never copied as keys.
                if not table.is_core:
                    parent_blocked = ('In an additional occurrence table, parentEventID describes an event, not the occurrence on the row. '
                                      'ChatIPT links parent events only from the main table, so this column is left out of the Data Package.')
                elif hierarchy_unsupported or not hierarchy['links']:
                    parent_blocked = hierarchy_unsupported or ((describe_problems(hierarchy) if hierarchy['problems'] else
                                                                'None of the values in parentEventID matches an eventID in your files.')
                                                               + ' The column is left out of the Data Package.')
                elif own == 'occurrence' and any(not row[core.terms.index(DWC + 'eventID')] or
                        missing_reference(row[core.terms.index(DWC + 'eventID')], ()) for row in core.rows):
                    parent_blocked = ('Some occurrences have no eventID, so their events cannot be identified and no parent links can be made. '
                                      'The column is left out of the Data Package; ChatIPT does not make up the missing events.')
                options, media_reason = ([], parent_blocked) if parent_blocked else ([PARENT_LINK], None)
                if not parent_blocked and hierarchy['problems']:
                    media_reason = (describe_problems(hierarchy)
                                    + ' The other links are made; these are listed in the report and left out of the Data Package.')
                if not parent_blocked and own == 'occurrence' and len({row[core.terms.index(DWC + 'eventID')] for row in core.rows}) != len(core.rows):
                    parent_default = 'preserve'
                    media_reason = (media_reason + ' ' if media_reason else '') + ('Parent links on occurrence rows work only when occurrences with the same '
                                    'eventID are combined into one event, and each parentEventID names one of those combined events. '
                                    'ChatIPT does not create parent events.')
            chosen = parent_default or special_chosen or (options[0] if options else "preserve")
            join_only = (table.loose and not table.is_core and term == DWC + ('eventID' if core.row_type == DWC + 'Event' else 'occurrenceID') and bool(table.ids)) or (family == 'assertion' and term == DWC + 'occurrenceID')
            if join_only:
                chosen = 'join'
            incompatible = {} if family == 'humboldt' else {
                target: sum(bool(_copy_rejection(table.row_type, target, value)) for value in values
                            if not (target.endswith(ASSERTION_IRI_FIELDS) and missing_reference(value)))
                for target in options if '.' in target}
            incompatible = {target: count for target, count in incompatible.items() if count}
            typed_reason = ("Some values do not fit where they would go: "
                            + ', '.join(f"{_target_words(target)} ({count} {'value' if count == 1 else 'values'})" for target, count in incompatible.items())
                            + '. Only the values that fit are copied; the others are listed in the report and stay only in your original files. '
                            'Leave the column out of the Data Package if you are not sure what it means.') if incompatible else None
            # Prefer an exact field on the declared subject over duplicate annotations
            # on identification/material fields. A different subject still needs review.
            exact_subject_default = False
            if (table.is_core or family == 'occurrence') and len(options) > 1 and not (
                    occurrence_context and term in {DWC + 'typeStatus', DWC + 'typeDesignationType'}):
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
            date_reason = ('Some event dates look like decimal years (such as 2015.5) or say the date is unknown (such as NA). They are copied '
                           'exactly as written, not corrected or treated as empty, so check what they mean.') if date_ambiguity else None
            review = bool(not join_only and values and (not options or (len(options) > 1 and not exact_subject_default) or media_reason or name_ambiguity or chosen.startswith('molecular-protocol.env_')) and automatic_family is None)
            non_preserve_options = [target for target in options if target != 'preserve']
            first_field = non_preserve_options[0].split('.', 1)[1] if non_preserve_options and '.' in non_preserve_options[0] else None
            glossary_family = dwca_glossary.family_of(first_field) if first_field else None
            option_notes = None
            if (table.is_core and core.row_type == DWC + 'Occurrence' and term == DWC + 'recordedBy' and
                    'event.eventConductedBy' in options):
                event_id = DWC + 'eventID'
                event_values = [row[table.terms.index(event_id)].strip() for row in table.rows] if event_id in table.terms else []
                nonempty_event_ids = [value for value in event_values if value]
                if not event_values or len(set(nonempty_event_ids)) == len(nonempty_event_ids):
                    option_notes = {'event.eventConductedBy': 'Each of your rows has its own event, so here this works like "Who saw or recorded the organism".'}
            generic_reason = ('This column could describe more than one thing, for example the occurrence or its identification. '
                              'Choose where it belongs, or leave it out of the Data Package.')
            item = {"id": _column_id(t, c), "table": t, "column": c, "term": term,
                    "default": chosen, "review": review, "options": ([{'value': 'join', 'label': 'Used to link these rows to the main table'}] if join_only else [_choice(value) for value in options] + ([] if chosen in {'occurrence.occurrenceStatus', 'event.eventCategory'} else [PRESERVE])),
                    "nonempty": len(values), "distinct": len(set(values)), "samples": [value[:500] for value in list(dict.fromkeys(values))[:3]],
                    **({'family': glossary_family} if glossary_family else {}),
                    **({'option_notes': option_notes} if option_notes else {}),
                    **({'default_when': default_when} if default_when else {}),
                    **({'follows_names': follows_names} if follows_names else {}),
                    **({'follows': f'material:{t}'} if chosen.startswith('material.') and not default_when else {}),
                    **({'incompatible_values': incompatible} if incompatible else {}),
                    **({'parent_link_unavailable': parent_blocked} if parent_blocked else {})}
            if parent_default:
                item['options'] = [PRESERVE] + [option for option in item['options'] if option['value'] != 'preserve']
            if sampling_label:
                item['options'] = [{**option, 'label': sampling_label} if option['value'] == 'derive' else option for option in item['options']]
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
            # individualCount has no column of its own, but convert() turns each whole-number count into the
            # occurrence's quantity (or a count assertion), whatever is chosen for the column.
            count_column = term == DWC + 'individualCount' and occurrence_context and not options and not join_only
            if count_column:
                item['unmapped'] = 'derived'
                item['options'] = [{**option, 'label': 'Used for the organism quantity'} if option['value'] == 'preserve' else option
                                   for option in item['options']]
            if qualifier_copy:
                # The qualifier follows the name text wherever verbatimIdentification is filled from scientificName.
                item['options'] = [{**option, 'label': 'Added after the name text in verbatimIdentification'} if option['value'] == 'preserve'
                                   else option for option in item['options']]
                item.pop('unmapped', None)
                item.update(verbatim_copy=('identification' if family == 'identification' else 'occurrence') + '.verbatimIdentification',
                            verbatim_role='qualifier')
                if VERBATIM_NAME in table.terms:
                    item['verbatim_source'] = _column_id(t, table.terms.index(VERBATIM_NAME))
            item.update(column_note(archive, t, c) or {})
            columns.append(item); profile["columns"].append({key: item[key] for key in ("term", "nonempty", "distinct", "samples")})
            if typed_reason or date_reason:
                warnings.append({'id': item['id'], 'title': short_term(term),
                    'reason': ' '.join(filter(None, (typed_reason, date_reason))), 'table': t,
                    'nonempty': len(values), 'samples': item['samples']})
            if review:
                title = short_term(term)
                reason = media_reason or (
                        "Some names look like they include an author, a qualifier such as 'cf.', or another unusual form. In a Darwin Core Data Package, scientificName holds only the name, without its author. "
                        + ("The full text fills verbatimIdentification wherever that would otherwise be empty, and your original files keep everything. "
                           if VERBATIM_NAME in table.terms else "The full text is always kept in verbatimIdentification. ")
                        + "Copy the names into scientificName as they are, or leave scientificName empty."
                        if name_ambiguity else ("Darwin Core Data Packages have no identificationQualifier field, and scientificName excludes qualifiers. "
                        "Each qualifier is added after the name text in verbatimIdentification wherever that is filled from scientificName, "
                        "so the uncertainty stays visible; your original files keep the column too.") if qualifier_copy
                        else COUNT_REASON if count_column
                        else generic_reason if options
                        else "The Darwin Core Data Package has no field for this column, so it is left out. Your download still has it in the copy of your original files." if term not in SCHEMA_TERMS
                        else "ChatIPT cannot convert this column yet, so it is left out of the Data Package. Your download still has it in the copy of your original files.")
                if reason == generic_reason and len(non_preserve_options) > 1:
                    title, family_reason = dwca_glossary.question(first_field, short_term(term))
                    reason = family_reason + ' ' + dwca_glossary.COPY_NOTE
                    item['family'] = glossary_family or 'other'
                issue = _issue(item["id"], title,
                    kind='name-semantics' if name_ambiguity and not media_reason else 'column-mapping', reason=reason,
                    options=item["options"], table=t, term=term, nonempty=len(values), samples=[value[:250] for value in item["samples"]],
                    **({'family': item['family']} if item.get('family') else {}),
                    **({'option_notes': option_notes} if option_notes else {}))
                non_preserve = [option['value'] for option in item['options'] if option['value'] != 'preserve']
                if has_material and non_preserve and all(target.startswith('material.') for target in non_preserve) and any(issue['id'] == f'material:{t}' for issue in issues):
                    issue['ask_when'] = [{'type': 'decision_in', 'id': f'material:{t}', 'values': ['per_row', 'by_id']}]
                issues.append(issue)
            if automatic_family and values:
                proposed_automatic.append({'id': item['id'], 'title': f"{table.name}: {short_term(term)}",
                    'reason': automatic_reason, 'options': item['options'], 'default': chosen,
                    'default_when': default_when,
                    'table': t, 'term': term, 'family': automatic_family, 'glance': glance,
                    **({'option_notes': option_notes} if option_notes else {}),
                    'nonempty': len(values), 'distinct': len(set(values)), 'samples': item['samples']})
            # Values the tidy-up dealt with (changed, or offered as a suggestion) are not asked about again.
            settled = settled_values(archive, t, c)
            if table.is_core and own == 'occurrence' and term == DWC + 'countryCode' and chosen == 'event.countryCode':
                for source_value, count in sorted(Counter(values).items()):
                    if not semantic_target_rejection('event.countryCode', source_value) or source_value in settled:
                        continue
                    issues.append(_issue(_reviewed_value_id('country-label', t, c, source_value),
                        f'{table.name}: where does {source_value[:100]!r} belong?',
                        f'{count} rows have {source_value[:200]!r} in countryCode, but it is not a two-letter ISO country code. '
                        'Choose whether it is the name of a country or of a water body, or leave it out of the Data Package. '
                        'The text is copied exactly as written; ChatIPT does not guess a country code.',
                        [PRESERVE, {'value': 'event.country', 'label': 'Country or territory name'},
                         {'value': 'event.waterBody', 'label': 'Water body name'}],
                        # Reading the supplier's own label into the field it names is an interpretation, not a new fact.
                        kind='column-mapping', table=t, source_column=c, source_value=source_value, count=count))
            if table.is_core and own == 'occurrence' and term == DWC + 'eventRemarks' and chosen == 'event.eventRemarks':
                event_ids = [row[table.terms.index(DWC + 'eventID')] for row in table.rows
                             if row[table.terms.index(DWC + 'eventID')]] if DWC + 'eventID' in table.terms else []
                if len(event_ids) == len(set(event_ids)):
                    for source_value, count in sorted(Counter(values).items()):
                        if not is_age_like_remark(source_value) or source_value in settled:
                            continue
                        issues.append(_issue(_reviewed_value_id('age-remark', t, c, source_value),
                            f'{table.name}: does {source_value[:100]!r} describe the organism?',
                            f'{count} rows have {source_value[:200]!r} in eventRemarks, which looks like it describes the organism rather than the event. '
                            'Choose what this text describes. As a life stage it is copied exactly as written, including abbreviations; '
                            'notes that mix counts, sex or eggs may fit better as an occurrence remark. The text itself is never changed.',
                            [{'value': 'event.eventRemarks', 'label': 'Keep as an event remark'},
                             {'value': 'occurrence.lifeStage', 'label': 'Life stage of the organism (as written)'},
                             {'value': 'occurrence.occurrenceRemarks', 'label': 'Occurrence remark (as written)'},
                             PRESERVE],
                            # Choosing the subject of the supplier's own words interprets them; nothing new is asserted.
                            kind='column-mapping', table=t, source_column=c, source_value=source_value, count=count))
        profiles.append(profile)
        if family == 'occurrence' and not table.is_core and core.row_type == DWC + 'Event':
            event_columns = [column for column in columns if column['table'] == t and column['nonempty'] and column['default'] != 'join'
                             and any(option['value'].startswith('event.') for option in column['options'])]
            # Only columns that go to the event by default are described; others are listed if chosen later.
            shown = [short_term(column['term']) for column in event_columns
                     if column['term'] != DWC + 'eventID' and column['default'].startswith('event.')]
            if shown:
                issues.append(_issue(f'occurrence-events:{t}', f'{table.name}: where and when details on occurrence rows',
                    f"{table.name} has event details on its rows ({', '.join(shown[:6])}{', …' if len(shown) > 6 else ''}). In a Data Package these belong to an event. "
                    f"They can be copied onto the linked event in {core.name} when all occurrences of that event agree, or each occurrence row can become its own event "
                    f"inside the linked event.",
                    [{'value': 'patch', 'label': f'Copy them onto the linked event in {core.name}'},
                     {'value': 'per-row', 'label': 'Make each occurrence row its own event, inside the linked event'},
                     {**PRESERVE, 'label': 'Leave these details out of the Data Package'}],
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
                    'reason': f'{partial} {"row has" if partial == 1 else "rows have"} only one usable coordinate (a latitude without a longitude, or the reverse). In {invalid} of them the other coordinate is not a valid value, so it was left out; the rest have none. The usable coordinate is copied on its own, without making a location point. The report lists the rows.',
                    'table': t, 'terms': pair, 'rows': partial, 'invalid_pairs': invalid})
        if family in {'germplasm-score', 'germplasm-trait'}:
            for n, row in enumerate(table.rows):
                source = _source(table.terms, row)
                if family == 'germplasm-score':
                    missing = not source.get(DWC + 'measurementValue', '').strip() or not (
                        source.get(DWC + 'measurementType', '').strip() or source.get(G + 'measurementTraitName', '').strip())
                    reason = 'This measurement row has no value, or no measurement type or trait name, so it cannot be converted. It is left out of the Data Package.'
                else:
                    missing = not any(source.get(term, '').strip() for term in (
                        G + 'measurementTraitName', DWC + 'measurementMethod', G + 'measurementTraitSource', G + 'measurementTraitRemarks'))
                    reason = 'This trait row has no name, method, source or remarks, so there is nothing to describe the trait. It is left out of the Data Package.'
                if missing:
                    row_issues.append(_issue(f'row:{t}:{n}', f'{table.name}, row {n + 1}: incomplete description', reason, [PRESERVE], table=t, row=n + 1))
        if family == 'molecular' and SEQUENCE in table.terms:
            # Rows without a sequence stay in the originals; with none at all, preflight leaves out the whole table.
            sequence = table.terms.index(SEQUENCE)
            missing = [n for n, row in enumerate(table.rows) if not row[sequence].strip()]
            if len(missing) < len(table.rows):
                for n in missing:
                    row_issues.append(_issue(f'row:{t}:{n}', f'{table.name}, row {n + 1}: no DNA sequence', SEQUENCELESS_DNA_ROW, [PRESERVE], table=t, row=n + 1))
        if family == 'eol-media':
            for n, source in enumerate(sources):
                reason = eol_media_row_review(source)
                if reason:
                    options = [PRESERVE]
                    if source.get(DCT + 'type', '').strip().lower() not in TEXT_TYPES:
                        options.append({'value': 'convert', 'label': 'Convert it as chosen for the table; details that cannot be converted stay only in your original files'})
                    row_issues.append(_issue(f'row:{t}:{n}', f'{table.name}, row {n + 1}: media meaning', reason, options, table=t, row=n + 1))
        if family == 'eol-reference':
            for n, source in enumerate(sources):
                if not source.get(DCT + 'identifier', '').strip():
                    row_issues.append(_issue(f'row:{t}:{n}', f'{table.name}, row {n + 1}: reference identifier is missing',
                        'This reference has no identifier, which EOL requires, so it is left out of the Data Package.', [PRESERVE], table=t, row=n + 1))
        if family in {'bmde', 'nbn'}:
            for n, source in enumerate(sources):
                reason = legacy_row_review(table.row_type, source)
                if reason:
                    options = [PRESERVE]
                    if family != 'bmde' or not (source.get(BMDE + 'LastModifiedAction', '').strip().upper() == 'DELETE' or source.get(BMDE + 'NoObservations', '').strip() == 'NoObs'):
                        options.append({'value': 'convert', 'label': 'Convert it as described; dates that are invalid or cannot be converted stay only in your original files'})
                    row_issues.append(_issue(f'row:{t}:{n}', f'{table.name}, row {n + 1}: record handling', reason, options, table=t, row=n + 1))
        if family == 'germplasm-score' and G + 'measurementTraitID' in table.terms:
            issues.append(_issue(f'trait-link:{t}', f'{table.name}: link measurements to their trait definitions?',
                'measurementTraitID may refer to a row of your trait table, or to a trait in an outside vocabulary. Each measurement can be '
                'linked to its trait definition only when its measurementTraitID matches exactly one row of a converted trait table.',
                [{**PRESERVE, 'label': 'Do not link them'}, {'value': 'exact', 'label': 'Link each measurement to the trait definition with the same ID'}], table=t))
        if family == 'humboldt':
            issues.append(_issue(f'hum-category:{t}', f'{table.name}: confirm survey events',
                'Survey descriptions (Humboldt) can only be attached to survey events. Events with no eventCategory can be marked as '
                'surveys if you confirm it; a different category in your files is never overwritten.',
                [{'value': 'require', 'label': 'Only if every linked event is already marked as a survey'},
                 {'value': 'confirm', 'label': 'These are survey events: mark events with no category as surveys'}], table=t))
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
                + (' The survey descriptions (Humboldt) can only be converted for survey events; otherwise they are left out of the Data Package.' if humboldt_present else ''),
                [{"value": "survey", "label": "Survey events (planned sampling or monitoring)"}, {"value": "occurrence", "label": "Observation context (where and when records were made)"}]))
        if own == "occurrence" and (table.is_core or family == "occurrence"):
            quantity = DWC + 'organismQuantity'
            quantity_type = DWC + 'organismQuantityType'
            unpaired = sum(bool(source.get(quantity)) != bool(source.get(quantity_type))
                           for row in table.rows for source in [_source(table.terms, row)])
            if unpaired:
                warnings.append({'id': f'quantity-pair:{t}', 'title': f'{table.name}: incomplete quantity descriptions',
                    'reason': f'{unpaired} {"row gives" if unpaired == 1 else "rows give"} an organismQuantity without an organismQuantityType, or the reverse. They are copied as they are; ChatIPT does not guess the missing part.',
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
            'Each exact name can be listed once as a person or organization and linked to all its mentions, or every name can stay as text only.',
            [{'value': 'shared', 'label': 'List each exact name once and link it to every mention'},
             {'value': 'text', 'label': 'Keep all names without identifiers as text only'}],
            kind='agent-identity', count=sum(agent_names.values()), names=len(agent_names)))
    for name, count in sorted(agent_names.items()):
        if count < 2:
            continue
        issues.append(_issue(_agent_name_id(name), f'Does {name[:100]!r} always mean the same person or organization?',
            f'{count} mentions in your files use this exact name. By default they are all linked to one person or organization. '
            'Keep the name as text only if it covers different people or organizations.',
            [{'value': 'shared', 'label': 'Link every mention to one person or organization'},
             {'value': 'separate', 'label': 'Keep this name as text only'}],
            kind='agent-identity', source_value=name, count=count))
    if core.row_type == DWC + 'Occurrence' and {DWC + 'organismQuantity', DWC + 'organismQuantityType'} <= set(core.terms):
        # Occurrences whose single DNA row has a sequence, and so becomes their one analysis.
        dna_rows = _dna_rows(archive)
        sequenced = {source_id for table in archive.tables if SUPPORTED_EXTENSIONS.get(table.row_type) == 'molecular' and SEQUENCE in table.terms
                     for source_id, row in zip(table.ids, table.rows) if row[table.terms.index(SEQUENCE)].strip()}
        quantity, quantity_type = core.terms.index(DWC + 'organismQuantity'), core.terms.index(DWC + 'organismQuantityType')
        reads = sum(dna_rows[source_id] == 1 and source_id in sequenced and _read_count(row[quantity], row[quantity_type]) is not None
                    for source_id, row in zip(core.ids, core.rows))
        if reads:
            issues.append(_issue(READ_COUNTS_ID, 'DNA read counts',
                f'{reads:,} {"occurrence gives its" if reads == 1 else "occurrences give their"} organismQuantity in DNA sequence reads and '
                f'{"has" if reads == 1 else "have"} one DNA row each. Each count can also be recorded as the read count of that DNA analysis; '
                'the occurrence keeps it either way.',
                [{'value': 'copy', 'label': 'Also record each count as the read count of its DNA analysis'},
                 {**PRESERVE, 'label': 'Keep read counts only on the occurrences'}],
                count=reads))
    for t in range(len(archive.tables)):
        followers = [column['id'] for column in columns if column['table'] == t and column.get('follows') == f'material:{t}' and column['nonempty'] > 0]
        material_issue = next((issue for issue in issues if issue['id'] == f'material:{t}'), None)
        if material_issue:
            material_issue['followers'] = followers
            names = [next(short_term(column['term']) for column in columns if column['id'] == identifier) for identifier in followers]
            if names:
                shown = ', '.join(names[:8]) + (f', and {len(names) - 8:,} more' if len(names) > 8 else '')
                material_issue['reason'] += f' If yes, these details are stored on the specimen records: {shown}. If no, they are left out of the Data Package.'
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
    for column in plan['columns']:
        # A column that is not asked about and lost every target to the data says why, rather than looking unmapped.
        if (not column.get('review') and column['nonempty'] and column.get('unavailable_options')
                and not any(option['value'] not in {'preserve', 'join'} for option in column['options'])):
            warnings.append({'id': column['id'], 'title': f"{archive.tables[column['table']].name}: {short_term(column['term'])}",
                             'reason': ' '.join(dict.fromkeys(option['reason'] for option in column['unavailable_options'])),
                             'table': column['table'], 'nonempty': column['nonempty']})
            column['unmapped'] = 'unavailable'
    for column in plan['columns']:
        # A name and its identifier stay on the same record: recordedByID may only go where recordedBy goes.
        for value, name_target in (column.get('follows_names') or {}).get('targets', {}).items():
            plan['requirements'].setdefault(column['id'], {}).setdefault(value, []).append({
                'conditions': [{'type': 'target_in', 'column': column['follows_names']['column'], 'targets': [name_target, 'preserve']}],
                'reason': f"recordedByID can only go where the names in recordedBy go ({_choice(name_target)['label'].lower()}), so each name "
                          'keeps its identifier. Change where recordedBy goes, or leave recordedByID out of the Data Package.'})
    _streamline_plan(archive, core, plan, warnings)
    for choice in proposed_automatic:
        column = next(column for column in plan['columns'] if column['id'] == choice['id'])
        choice['options'] = column['options']
    plan['automatic_choices'].extend(proposed_automatic)
    _require_valid_defaults(plan)
    apply_policy(plan['issues']); apply_policy(plan['automatic_choices'])
    plan['glossary'] = _glossary(plan)
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
                   if failed_requirements(plan, effective, choice['id'], effective.get(choice['id'], choice['default']))]
        if not failing:
            return
        for choice in failing:
            value = effective.get(choice['id'], choice['default'])
            reasons = ' '.join(requirement['reason'] for requirement in failed_requirements(plan, effective, choice['id'], value))
            plan['automatic_choices'].remove(choice)
            plan['warnings'] = [warning for warning in plan['warnings'] if warning['id'] != choice['id']]
            question = {key: item for key, item in choice.items() if key not in {'default', 'question_reason', 'default_when'}}
            plan['issues'].append(question | {'reason': choice.get('question_reason', choice['reason']) + ' ' + reasons})
            for column in plan['columns']:
                if column['id'] == choice['id']:
                    column['review'] = True
                    column.pop('default_when', None)


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

    unresolved = [item["id"] for item in plan["issues"] if item['id'] not in nested_ids and asked(plan, effective, item) and not resolved(item) and not (
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
        raise ConversionError(f"Answer the {len(unresolved)} remaining {'question' if len(unresolved) == 1 else 'questions'} before converting.", category='decision',
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
            raise ConversionError('Parent event links on occurrence rows work only when occurrences with the same eventID are combined '
                                  'into one event. With a separate event for each row, leave parentEventID out of the Data Package instead.',
                                  category='decision', decision_ids=['event-grain', column['id']])
        if unsupported or not result['links']:
            raise ConversionError(unsupported or (describe_problems(result) if result['problems'] else 'None of the values in parentEventID matches an eventID in your files.'),
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
        raise ConversionError("The conversion rules or the tidy-up of your files changed after they were checked. Inspect them again to continue.", category='stale-plan')
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
    # Read counts of core occurrences with exactly one DNA row, by core id; analyses that received one.
    dna_rows = _dna_rows(archive); read_counts = {}; copied_read_counts = 0
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
                raise ConversionError(f"Two columns give different values for the {_target_words(target)} on the same row. Leave one of them out of the Data Package, or correct your files.", category='conflict',
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
            raise ConversionError('An additional table describes an event that is not in the main table.', category='internal')
        for field, value in record.items():
            if value and event.get(field) and event[field] != value:
                raise ConversionError(f"{archive.tables[t].name}, row {n + 1}: its {dwca_glossary.field_label(field)} differs from the event's. Leave that column out of the Data Package, or correct your files.",
                                      category='conflict', decision_ids=[f'table:{t}', *patch_columns(t, field)],
                                      evidence={'source_table': archive.tables[t].name, 'source_row': n + 1, 'field': field,
                                                'event_value': event[field], 'extension_value': value})
        combined = {**event, **{field: value for field, value in record.items() if value}}
        if combined.get('year') and combined.get('eventDate'):
            if not re.fullmatch(r'-?\d{1,4}', combined['year']):
                raise ConversionError('A year must be a whole number, such as 2015.', category='conflict',
                                      decision_ids=[f'table:{t}', *patch_columns(t, 'year')], evidence={'source_row': n + 1})
            if _year_disagrees(combined['year'], combined['eventDate']):
                raise ConversionError("A year in an additional table does not match the event's eventDate.", category='conflict',
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
        if name.endswith('-assertion') and row.get('verbatimAssertionType') and not row.get('assertionType'):
            # The supplier's own type is its category: keep it verbatim and as assertionType (the
            # schema uses the verbatim field in addition to assertionType). A supplied one is never replaced.
            row['assertionType'] = row['verbatimAssertionType']
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
        if t == core_index and decisions.get(READ_COUNTS_ID) == 'copy' and dna_rows[source_id] == 1:
            # From the converted pair, so a quantity column left out of the package is not copied either.
            reads = _read_count(occurrence.get('organismQuantity', ''), occurrence.get('organismQuantityType', ''))
            if reads is not None:
                read_counts[source_id] = reads
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
                raise ConversionError('To combine specimen records by identifier, every row needs a specimen identifier. Choose one specimen record per row instead, or fill in the missing identifiers.', category='decision',
                                      decision_ids=[f'material:{t}'])
            # evidenceForOccurrenceID shares a source IRI with occurrenceID, but is a relationship, not a copied identifier.
            material.pop('evidenceForOccurrenceID', None)
            material.update(materialEntity_pk=_key(archive, 'material', *group), collectionEvent_fk=event_key)
            if group in material_groups and material_groups[group] != material:
                raise ConversionError(f'Rows with specimen identifier {group[1]!r} disagree about the specimen or where it was collected. Choose one specimen record per row, or correct your files.',
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

    sampling_columns = [column for column in columns_by_table[core_index]
                        if column['term'] in SAMPLING_TERMS and decisions.get(column['id'], column['default']) == 'derive']
    sampling_events, sampling_signatures, sampling_protocols, protocol_links = {}, {}, {}, set()
    protocol_targets = Counter()  # Source rows per protocol field, for the report.

    def withhold_sampling(n, term, value, reason):
        withheld_values.append({'source_table': core.name, 'source_table_index': core_index, 'source_row': n + 1,
                                'term': term, 'value': value, 'target': 'derive', 'reason': reason})

    def core_sampling(n, row, group_id, event_key):
        """A core row's samplingProtocol and sample size describe the event that holds its sampling: the row's
        own event, the event rows are combined into, or the depth child of a depth split."""
        supplied = {column['term']: row[column['column']] for column in sampling_columns}
        signature = sampling_signatures.setdefault(event_key, supplied)
        if signature != supplied:
            raise ConversionError(f'Rows combined into event {group_id!r} give different sampling protocols or sample sizes. '
                                  'Keep each row as its own event, or leave these columns out of the Data Package.',
                                  category='conflict', decision_ids=['event-grain', *(column['id'] for column in sampling_columns)],
                                  evidence={'eventID': group_id, 'source_row': n + 1,
                                            'terms': sorted(term.rsplit('/', 1)[-1] for term in supplied if supplied[term] != signature.get(term))})
        entry = sampling_events.setdefault(event_key, {'rows': [], 'protocol': '', 'protocols': [], 'size': None})
        entry['rows'].append(n)
        protocol = supplied.get(SAMPLING_PROTOCOL, '')
        # Darwin Core lists use ' | '; each listed protocol is a record of its own, shared by exact text.
        items = list(dict.fromkeys(part for part in protocol.split(' | ') if part.strip()))
        if protocol and not items:
            withhold_sampling(n, SAMPLING_PROTOCOL, protocol, 'This samplingProtocol has no text between its " | " separators, '
                                                               'so it stays in your original files.')
        if items:
            entry['protocol'] = protocol
            entry['protocols'] = []
            protocol_targets.update({'protocol.' + _protocol_field(item) for item in items})
            for item in items:
                key = sampling_protocols.get(item)
                if key is None:
                    # Nested Taxon-core occurrence tables share the archive fingerprint; their names keep keys apart.
                    key = sampling_protocols[item] = _key(archive, 'sampling-protocol', core.name, item)
                    add('protocol', {'protocol_pk': key, _protocol_field(item): item}, core_index, n)
                else:
                    trace('protocol', {'protocol_pk': key}, core_index, n)
                if (event_key, key) in protocol_links:
                    trace('event-protocol', {'event_fk': event_key, 'protocol_fk': key}, core_index, n)
                else:
                    protocol_links.add((event_key, key))
                    add('event-protocol', {'event_fk': event_key, 'protocol_fk': key}, core_index, n)
                entry['protocols'].append(key)
            column_consumption[(core_index, SAMPLING_PROTOCOL)].add(n)
        value, unit = (supplied.get(term, '') for term in SAMPLE_SIZE)
        if value and unit:
            entry['size'] = (value, unit)  # Written once all events are known: survey record or event assertion.
        elif value or unit:
            withhold_sampling(n, SAMPLE_SIZE[0] if value else SAMPLE_SIZE[1], value or unit,
                              'sampleSizeValue and sampleSizeUnit only make sense together, and this row has only one of them, '
                              'so it stays in your original files.')

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
                raise ConversionError("To combine occurrences into events by eventID, every row of the main table needs an eventID.", category='decision',
                                      decision_ids=['event-grain'])
        event_key = _key(archive, "event", group_id); event_keys[source_id] = group_keys[source_id] = event_key
        event.update(event_pk=event_key)
        if not event.get("eventCategory"):
            event["eventCategory"] = "occurrence" if core.row_type == DWC + "Occurrence" else decisions.get("event-category", "")
        if DWC + 'eventCategory' in core.terms and row[core.terms.index(DWC + 'eventCategory')]:
            supplied_categories.add(event_key)
        if group_id in event_groups:
            if event_groups[group_id] != event:
                raise ConversionError(f"Rows with eventID {group_id!r} disagree about the event, for example its date or place. Give each row its own event, or correct your files.",
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
        if sampling_columns:
            core_sampling(n, row, group_id, event_key)
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
                raise ConversionError('Survey descriptions on occurrence rows need the occurrences with the same eventID combined into one event.',
                                      category='decision', decision_ids=['event-grain', f'table:{t}'])
            expected, attached, signatures = defaultdict(set), defaultdict(set), defaultdict(set)
            for source_id in core.ids: expected[event_keys[source_id]].add(source_id)
            for row, source_id in zip(table.rows, table.ids):
                event_key = event_keys[source_id]; attached[event_key].add(source_id); signatures[event_key].add(source_signature(t, row))
            if any(attached[key] != expected[key] or len(signatures[key]) != 1 for key in attached):
                raise ConversionError('To make one survey per eventID, every occurrence with that eventID needs a survey row, and those rows must be identical.',
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
                            raise ConversionError(f'A measurement names occurrenceID {explicit_id!r}, which does not match exactly one occurrence in its event.',
                                                  category='conflict', decision_ids=[f'table:{t}'],
                                                  evidence={'source_table': table.name, 'source_row': n + 1, 'matches': len(matches)})
                        record['occurrence_fk'] = matches[0][0]; target = 'occurrence-assertion'
                    elif role == 'occurrence-assertion':
                        if explicit_id:
                            matches = occurrences_by_identifier.get(explicit_id, [])
                            if len(matches) != 1 or matches[0][0] != occurrence_keys[source_id]:
                                raise ConversionError('A measurement names an occurrenceID that does not belong to the record it is attached to.', category='conflict',
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
                            raise ConversionError('Survey descriptions need survey events, but this event has a different eventCategory in your files, which ChatIPT does not overwrite.',
                                                  category='decision', decision_ids=[f'hum-category:{t}', 'event-category', f'table:{t}'])
                        event['eventCategory'] = 'survey'
                    source = approved_source(t, row)
                    scope_decision = decisions.get(f'hum-scope:{t}:{n}', 'source')
                    group = (t, event_key) if role in {'humboldt-grouped', 'humboldt-merge'} else (t, n)
                    signature = (source_signature(t, row), json.dumps(mapped, sort_keys=True), scope_decision)
                    if group in humboldt_groups and humboldt_groups[group][0] != signature:
                        first_row = humboldt_groups[group][2]
                        raise ConversionError('Survey rows can be combined into one survey only when they are identical, with the same answers about scope. Keep them as separate surveys instead.',
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
                            raise ConversionError('Passport details can only be attached to specimen records, so every linked row of the main table needs a specimen record with an identifier.',
                                                  category='conflict', decision_ids=[f'table:{t}', f'material:{core_index}'],
                                                  evidence={'source_table': table.name, 'source_row': n + 1})
                        subject_key = material['materialEntity_pk']
                        if family == 'germplasm-score' and (not raw.get(G + 'germplasmID') or raw[G + 'germplasmID'] not in material_identifiers[subject_key]):
                            raise ConversionError("A measurement's germplasmID does not match the accession number of its specimen record.",
                                                  category='conflict', decision_ids=[f'table:{t}', f'material:{core_index}'],
                                                  evidence={'source_table': table.name, 'source_row': n + 1, 'germplasmID': raw.get(G + 'germplasmID', '')})
                    elif subject_table == 'protocol': subject_key = _key(archive, 'trait-protocol', t, n)
                    elif subject_table == 'event': subject_key = event_keys[source_id]
                    else: subject_key = occurrence_keys[source_id]
                    records = emit_germplasm_records(family, derived, mapped, subject_table, subject_key, _key(archive, 'germplasm-reference', t, n))
                    if family == 'germplasm-score' and decisions.get(f'trait-link:{t}') == 'exact' and raw.get(G + 'measurementTraitID'):
                        matches = trait_protocols[raw[G + 'measurementTraitID']]
                        if len(matches) != 1:
                            raise ConversionError("A measurement's measurementTraitID does not match exactly one converted trait definition, so it cannot be linked.",
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
                            raise ConversionError('The NBN date columns belong together: convert them all, or leave them all out of the Data Package.', category='decision',
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
                        raise ConversionError('A media row has no values that can be converted. Leave the media table out of the Data Package instead of creating empty media records.',
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
                        raise ConversionError("Every DNA row needs a DNA sequence to be converted.", category='conflict',
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
                    analysis = {"nucleotideAnalysis_pk": _key(archive, "analysis", t, n), "nucleotideSequence_fk": sequence_key,
                                "molecularProtocol_fk": protocol_key, "event_fk": event_keys[source_id]}
                    if source_id in read_counts:
                        analysis['readCount'] = read_counts[source_id]
                        copied_read_counts += 1
                    add("nucleotide-analysis", analysis, t, n)
            except ConversionError:
                raise
            except ImportFailure as error:
                # Emitter and raw-source failures for one extension row: retaining the table or row remedies them.
                raise ConversionError(f'{table.name}, row {n + 1}: {error}', category='conflict',
                                      decision_ids=[f'table:{t}', *([f'row:{t}:{n}'] if f'row:{t}:{n}' in issues_by_id else [])],
                                      evidence={'source_table': table.name, 'source_row': n + 1}) from error
    # A core sample size goes to the survey record of a survey event. When converted Humboldt rows
    # already describe that event's survey (possibly several), or the event is not a survey, it is an
    # event assertion instead, so no survey is merged into another or created for a non-survey event.
    humboldt_events = {survey['event_fk'] for survey in resources.get('survey', [])}
    sample_size_routes = Counter()
    for event_key, entry in sampling_events.items():
        if not entry['size']:
            continue
        (value, unit), (first, *others) = entry['size'], entry['rows']
        if events_by_key[event_key].get('eventCategory') == 'survey' and event_key not in humboldt_events:
            record = {'survey_pk': _key(archive, 'core-survey', event_key), 'event_fk': event_key,
                      'sampleSizeValue': value, 'sampleSizeUnit': unit}
            if entry['protocol']:
                record['samplingProtocol'] = entry['protocol']
                protocol_targets['survey.samplingProtocol'] += len(entry['rows'])
                if len(entry['protocols']) == 1:
                    record['samplingProtocol_fk'] = entry['protocols'][0]
            name, route = 'survey', 'survey'
        else:
            record = {'event_fk': event_key, 'assertionType': 'sampleSizeValue', 'assertionTypeIRI': SAMPLE_SIZE[0],
                      'assertionValue': value, 'assertionUnit': unit}
            name, route = 'event-assertion', 'assertion'
        add(name, record, core_index, first)
        for n in others:
            trace(name, {'survey_pk': record['survey_pk']} if name == 'survey' else {'event_fk': event_key}, core_index, n)
        for n in entry['rows']:
            for term in SAMPLE_SIZE: column_consumption[(core_index, term)].add(n)
        sample_size_routes[route] += len(entry['rows'])
    # Tables whose rows became occurrences, so individualCount was considered for every converted row.
    count_tables = [t for t, table in enumerate(archive.tables) if table.row_type == DWC + 'Occurrence'
                    and DWC + 'individualCount' in table.terms and (table.is_core or decisions.get(f'table:{t}') == 'occurrence')]
    for column in plan["columns"]:
        source_table = archive.tables[column["table"]]
        if column['term'] == DWC + 'individualCount' and column['table'] in count_tables:
            continue  # Replaced below with the row-level derived disposition.
        target = decisions.get(column["id"], column["default"])
        if column in sampling_columns:
            consumed = len(column_consumption[(column['table'], column['term'])])
            if column['term'] == SAMPLING_PROTOCOL:
                target_counts = {'event-protocol.protocol_fk': consumed, **dict(sorted(protocol_targets.items()))}
                rule = ('Each item of a " | " list becomes a protocol record (protocolName, or protocolReferences for a single http(s) '
                        'link), shared by exact text and linked to its event through event-protocol. A survey record created for '
                        'the sample size also keeps the full text, and links the protocol when there is only one.')
            else:
                field = 'Value' if column['term'] == SAMPLE_SIZE[0] else 'Unit'
                target_counts = {target: sample_size_routes[route] for route, target in (
                    ('survey', f'survey.sampleSize{field}'), ('assertion', f'event-assertion.assertion{field}')) if sample_size_routes[route]}
                rule = ('A paired sampleSizeValue and sampleSizeUnit go to the survey record of a survey event that no converted '
                        'Humboldt row describes; otherwise they become an event-assertion with assertionType sampleSizeValue. '
                        'Values are copied unchanged; an unpaired value or unit stays in the originals.')
            dispositions.append({'source_table': source_table.name, 'term': column['term'],
                                 'target': 'derived sampling records' if consumed else 'preserve',
                                 'disposition': 'derived' if consumed else 'retained-unmapped',
                                 'nonempty': column['nonempty'], 'mapped_rows': consumed,
                                 'retained_only_rows': column['nonempty'] - consumed,
                                 **({'target_counts': target_counts, 'mapping_rule': rule} if consumed else {})})
            continue
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
        if target.endswith('verbatimAssertionType'):
            # add() also fills assertionType with the same text wherever none was supplied.
            extra['also_copied_to'] = target[:-len('verbatimAssertionType')] + 'assertionType'
        if column['table'] == core_index and column['term'] == DWC + 'organismQuantity' and copied_read_counts:
            extra.update(also_copied_to='nucleotide-analysis.readCount', also_copied_rows=copied_read_counts)
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
        raise ConversionError('The same surveyID would be used for several separate surveys. Combine identical survey rows for each event, or leave surveyID out of the Data Package.',
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
