"""Project DwC-DP nucleotide analyses into GBIF's DNA Derived Data extension.

The links follow GBIF's own DwC-DP ingestion (gbif/pipelines NucleotideMapping, 8ff035d):
an Occurrence core takes the analyses of its single evidence material
(material.evidenceForOccurrenceID), and an Event core takes the analyses made directly on
an event (event_fk set, materialEntity_fk blank). ChatIPT adds the one link metabarcoding
needs and GBIF's DwC-DP path lacks: a detection is tied to its own analysis only by the
identification based on it (identification.occurrence_fk with nucleotideAnalysis_fk). An
analysis with such a link follows it alone, so the material fallback never also attaches it
to the sample's evidence occurrence. No link is guessed.
"""

from collections import Counter, defaultdict
from xml.etree import ElementTree as ET

import pandas as pd

from api.dwc_dp_specs import TABLE_SPECS
from api.dwc_specs import DarwinCoreCoreType, DarwinCoreExtensionType, EXTENSION_SCHEMAS


CORE_ID_COLUMN = '_coreid'
SEQUENCE_IRI = 'http://rs.gbif.org/terms/dna_sequence'
ASSERTION_RESOURCES = ('nucleotide-analysis-assertion', 'molecular-protocol-assertion')
# Analyses that produce no row, by reason; the wording is shown to the agent.
REASONS = {
    'no_link': 'no identification, evidence material or event links them to a core record',
    'ambiguous_material': ('their material is evidence for an occurrence that has several evidence materials, '
                           'or for an occurrenceID that several occurrences share'),
    'material_on_event_core': ('they belong to a material; an Event core holds only analyses made directly '
                               'on an event, while an Occurrence core keeps them'),
    'not_in_core': 'their linked core record is not in the core table',
    'ambiguous_core_id': ("their linked core record's identifier could also be another record's "
                          "(one record's public ID equals another's _pk)"),
    'no_dna_facts': 'their sequence and protocol hold no DNA Derived Data term',
}


def _extension_terms():
    root = ET.parse(EXTENSION_SCHEMAS[DarwinCoreExtensionType.DNA_DERIVED_DATA].spec_path).getroot()
    return {element.attrib['qualName']: element.attrib['name'] for element in root
            if element.tag.rsplit('}', 1)[-1] == 'property' and element.attrib.get('qualName')}


_TERMS = _extension_terms()
SEQUENCE_TERM = _TERMS[SEQUENCE_IRI]
# molecular-protocol fields matched to extension terms by IRI, which also gives the renamed ones
# (samp_collect_device -> samp_collec_device, associated_resource -> url). GBIF takes sequences only
# from nucleotide-sequence, so the protocol's own DNA_sequence is left out.
PROTOCOL_TERMS = {
    field['name']: _TERMS[field['dcterms:isVersionOf']]
    for field in TABLE_SPECS['molecular-protocol'].schema['fields']
    if not field['name'].endswith(('_pk', '_fk')) and _TERMS.get(field.get('dcterms:isVersionOf')) not in (None, SEQUENCE_TERM)
}
# Populated values of these stay in the DwC-DP only: the extension has no term for them.
UNREPRESENTABLE = {
    resource: [field['name'] for field in TABLE_SPECS[resource].schema['fields']
               if not field['name'].endswith(('_pk', '_fk')) and field['name'] not in mapped]
    # The protocol's DNA_sequence is reported on its own as deliberately skipped.
    for resource, mapped in (('nucleotide-analysis', ()), ('nucleotide-sequence', ('sequence',)),
                             ('molecular-protocol', (*PROTOCOL_TERMS, 'DNA_sequence')))
}


def _text(frame, column):
    if column not in frame:
        return pd.Series('', index=frame.index, dtype='string')
    return frame[column].astype('string').fillna('').str.strip()


def _core_identifiers(subjects, pk, public, core_ids, id_mode):
    """Map each subject `_pk` to the one core identifier that belongs to it.

    id_mode 'pk' says the core carries `_pk` values (the provisional archive). Otherwise a core
    identifier may be a record's public ID or its `_pk`, since an agent-built core can use either.
    It is attached only when exactly one record could own it and that record could own no other:
    one record's occurrenceID equal to another's occurrence_pk leaves both unattached.
    """
    owners = defaultdict(set)
    for key, public_id in zip(_text(subjects, pk), _text(subjects, public)):
        if key:
            for value in ({key} if id_mode == 'pk' else {key, public_id} - {''}) & core_ids:
                owners[value].add(key)
    claims = defaultdict(set)
    for value, keys in owners.items():
        for key in keys:
            claims[key].add(value)
    mapping, ambiguous = {}, set()
    for key, values in claims.items():
        if len(values) == 1 and len(owners[next(iter(values))]) == 1:
            mapping[key] = next(iter(values))
        else:
            ambiguous.add(key)
    return mapping, ambiguous


def _occurrence_links(resources, analysis_keys):
    """Yield (occurrence_pk, analysis_pk) pairs and the reason each unlinked analysis has none."""
    pairs, explicit = [], set()
    identification = resources.get('identification')
    if identification is not None:
        occurrences, analyses = _text(identification, 'occurrence_fk'), _text(identification, 'nucleotideAnalysis_fk')
        for occurrence, analysis in zip(occurrences, analyses):
            if occurrence and analysis in analysis_keys:
                pairs.append((occurrence, analysis))
                explicit.add(analysis)

    # Material key -> its evidence occurrence's `_pk`, kept only when that link is one-to-one.
    evidence_material, ambiguous = {}, set()
    material, occurrence = resources.get('material'), resources.get('occurrence')
    if material is not None and occurrence is not None:
        evidence = defaultdict(list)
        for key, target in zip(_text(material, 'materialEntity_pk'), _text(material, 'evidenceForOccurrenceID')):
            if key and target:
                evidence[target].append(key)
        keys, publics = _text(occurrence, 'occurrence_pk'), _text(occurrence, 'occurrenceID')
        shared = Counter(publics[keys.ne('')])
        for key, public in zip(keys, publics):
            materials = evidence.get(public, []) if key and public else []
            if len(materials) == 1 and shared[public] == 1:
                evidence_material[materials[0]] = key
            else:
                ambiguous.update(materials)

    reasons = {}
    analyses = resources['nucleotide-analysis']
    for key, material_key in zip(_text(analyses, 'nucleotideAnalysis_pk'), _text(analyses, 'materialEntity_fk')):
        if not key or key in explicit:
            continue
        if material_key in evidence_material:
            pairs.append((evidence_material[material_key], key))
        else:
            reasons[key] = 'ambiguous_material' if material_key in ambiguous else 'no_link'
    return pairs, reasons


def _event_links(resources):
    pairs, reasons = [], {}
    analyses = resources['nucleotide-analysis']
    for key, event, material in zip(_text(analyses, 'nucleotideAnalysis_pk'), _text(analyses, 'event_fk'),
                                    _text(analyses, 'materialEntity_fk')):
        if not key:
            continue
        if material:
            reasons[key] = 'material_on_event_core'
        elif event:
            pairs.append((event, key))
        else:
            reasons[key] = 'no_link'
    return pairs, reasons


def _populated(frame, columns):
    return {column: int(count) for column in columns if column in frame and (count := _text(frame, column).ne('').sum())}


def dna_derived_data(resources, core_type, core_ids, id_mode=None):
    """Return (DNA Derived Data rows keyed by `_coreid`, or None, and a report).

    resources maps DwC-DP resource names to DataFrames; core_ids are the core table's identifiers.
    Each (core record, analysis) link gives one row.
    """
    core_type = DarwinCoreCoreType(core_type)
    analyses = resources.get('nucleotide-analysis')
    report = {'analyses': 0, 'rows': 0, 'core_records': 0, 'not_projected': {}, 'dropped_links': {},
              'missing_references': {}, 'skipped_protocol_sequences': 0, 'kept_in_dwc_dp_only': {}, 'assertions': {}}
    if analyses is None or analyses.empty:
        return None, report
    analysis_keys = set(_text(analyses, 'nucleotideAnalysis_pk')) - {''}
    report['analyses'] = len(analysis_keys)
    if core_type == DarwinCoreCoreType.OCCURRENCE:
        pairs, reasons = _occurrence_links(resources, analysis_keys)
        subjects, pk, public = resources.get('occurrence'), 'occurrence_pk', 'occurrenceID'
    elif core_type == DarwinCoreCoreType.EVENT:
        pairs, reasons = _event_links(resources)
        subjects, pk, public = resources.get('event'), 'event_pk', 'eventID'
    else:
        report['not_projected'] = {'core_type': len(analysis_keys)}
        return None, report
    core_ids = {str(value).strip() for value in core_ids} - {''}
    mapping, ambiguous = (_core_identifiers(subjects, pk, public, core_ids, id_mode)
                          if subjects is not None else ({}, set()))

    sequences, protocols = resources.get('nucleotide-sequence'), resources.get('molecular-protocol')
    sequence_by_key = {}
    if sequences is not None:
        sequence_by_key = {key: value for key, value in zip(_text(sequences, 'nucleotideSequence_pk'),
                                                           _text(sequences, 'sequence')) if key}
    protocol_rows = {}
    if protocols is not None:
        for key, (_, row) in zip(_text(protocols, 'molecularProtocol_pk'), protocols.iterrows()):
            if key:
                protocol_rows[key] = {term: row[field] for field, term in PROTOCOL_TERMS.items()
                                      if field in row.index and not pd.isna(row[field]) and str(row[field]).strip()}
        report['skipped_protocol_sequences'] = int(_text(protocols, 'DNA_sequence').ne('').sum())

    facts, missing = {}, Counter()
    for key, sequence_key, protocol_key in zip(_text(analyses, 'nucleotideAnalysis_pk'),
                                               _text(analyses, 'nucleotideSequence_fk'),
                                               _text(analyses, 'molecularProtocol_fk')):
        if not key:
            continue
        row = dict(protocol_rows.get(protocol_key, {}))
        if sequence_by_key.get(sequence_key):
            row[SEQUENCE_TERM] = sequence_by_key[sequence_key]
        if sequence_key not in sequence_by_key:
            missing['sequence'] += 1
        if protocol_key not in protocol_rows:
            missing['protocol'] += 1
        facts[key] = row
    report['missing_references'] = dict(missing)

    rows, linked, dropped = [], set(), []
    # Several identifications of one detection can rest on the same analysis; each core
    # identifier has one owner, so a distinct pair is a distinct row.
    for subject, analysis in dict.fromkeys(pairs):
        if subject in ambiguous:
            dropped.append((analysis, 'ambiguous_core_id'))
        elif subject not in mapping:
            dropped.append((analysis, 'not_in_core'))
        elif not facts[analysis]:
            dropped.append((analysis, 'no_dna_facts'))
        else:
            rows.append({CORE_ID_COLUMN: mapping[subject], **facts[analysis]})
            linked.add(analysis)
    for analysis, reason in dropped:
        reasons.setdefault(analysis, reason)
    # Analyses with no row at all, and the links dropped from analyses that still have one.
    report['not_projected'] = dict(Counter(reason for key, reason in reasons.items() if key not in linked))
    report['dropped_links'] = dict(Counter(reason for analysis, reason in dropped if analysis in linked))
    report['rows'] = len(rows)
    report['core_records'] = len({row[CORE_ID_COLUMN] for row in rows})
    for resource, columns in UNREPRESENTABLE.items():
        if resources.get(resource) is not None:
            report['kept_in_dwc_dp_only'].update(
                {f'{resource}.{column}': count for column, count in _populated(resources[resource], columns).items()})
    report['assertions'] = {name: len(resources[name]) for name in ASSERTION_RESOURCES
                            if resources.get(name) is not None and not resources[name].empty}
    if not rows:
        return None, report
    frame = pd.DataFrame(rows)
    return frame[[CORE_ID_COLUMN] + [column for column in frame.columns if column != CORE_ID_COLUMN]], report


def describe(report):
    """A short account of the DNA Derived Data projection for the agent."""
    lines = [f"DNA Derived Data: {report['rows']} row(s) for {report['core_records']} core record(s) "
             f"from {report['analyses']} DwC-DP nucleotide analyses, built by ChatIPT from the DwC-DP."]
    for reason, count in report['not_projected'].items():
        if reason == 'core_type':
            lines.append(f"- {count} analyses were not projected: DNA Derived Data needs an Occurrence or Event core.")
        else:
            lines.append(f"- {count} analyses were not projected: {REASONS[reason]}.")
    for reason, count in report['dropped_links'].items():
        lines.append(f"- {count} further link(s) of projected analyses were dropped: {REASONS[reason]}.")
    for reference, count in report['missing_references'].items():
        lines.append(f"- {count} analyses have no {reference} row to project (blank or unresolved reference).")
    if report['skipped_protocol_sequences']:
        lines.append(f"- {report['skipped_protocol_sequences']} molecular-protocol DNA_sequence value(s) were not "
                     "projected: sequences are taken only from nucleotide-sequence.")
    if report['kept_in_dwc_dp_only']:
        fields = ', '.join(f'{field} ({count})' for field, count in report['kept_in_dwc_dp_only'].items())
        lines.append(f"- No DNA Derived Data term exists for these populated fields, which stay in the DwC-DP: {fields}.")
    if report['assertions']:
        names = ', '.join(f'{name} ({count})' for name, count in report['assertions'].items())
        lines.append(f"- DNA Derived Data does not carry {names}; project them through (Extended)MeasurementOrFact.")
    if report['not_projected'] or report['dropped_links'] or report['missing_references']:
        lines.append("Fix a missing DwC-DP link only where the source establishes it.")
    if len(lines) > 1:
        lines.append("Record what was not projected in the projection coverage notes.")
    return '\n'.join(lines)
