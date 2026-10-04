"""Pure helpers for the EOL Media and EOL References extensions.

Audited against c34fa59ea7e7-media_extension.xml, 650d0e7712e0-reference_extension.xml and DwC-DP schema revision
76898192fd298c2aa170a7059e1bdadf3ee2a828 (docs/dwca-conversion/cloud-audits/remaining-fields.md). Only registered
term IRIs are planned; EOL-namespace terms are never treated as their Audubon/DwC namesakes. No network or Django state.
"""
from __future__ import annotations

from api.dwca_import import ImportFailure
from api.dwca_media import absolute_iri, media_targets

DCT = 'http://purl.org/dc/terms/'
AC = 'http://rs.tdwg.org/ac/terms/'
BIBO = 'http://purl.org/ontology/bibo/'
XMP = 'http://ns.adobe.com/xap/1.0/'
IPTC = 'http://iptc.org/std/Iptc4xmpExt/1.0/xmlns/'
EOL_MEDIA = 'http://eol.org/schema/media/Document'
EOL_REFERENCE = 'http://eol.org/schema/reference/Reference'
EOL_FAMILIES = {EOL_MEDIA: 'eol-media', EOL_REFERENCE: 'eol-reference'}
LEGACY_SUBTYPE = 'http://rs.tdwg.org/audubon_core/subtype'
SPM_INFO_ITEMS = 'http://rs.tdwg.org/ontology/voc/SPMInfoItems'
TEXT_TYPES = {'text', 'http://purl.org/dc/dcmitype/text'}
REFERENCE_SUBJECTS = {'occurrence': 'occurrence_fk', 'event': 'event_fk',
                      'material': 'materialEntity_fk', 'protocol': 'protocol_fk'}

# EOL media terms whose audited definitions agree with the shared Audubon/Simple Multimedia planner.
_SHARED_MEDIA_TERMS = {DCT + name for name in ('identifier', 'type', 'format', 'title', 'description', 'modified', 'rights',
                                              'bibliographicCitation', 'creator')} | {
    AC + 'accessURI', AC + 'furtherInformationURL', AC + 'derivedFrom', XMP + 'CreateDate', XMP + 'Rating',
    XMP + 'rights/UsageTerms', XMP + 'rights/Owner'}
_MEDIA_PRESERVED = {
    'http://rs.tdwg.org/dwc/terms/taxonID': 'A taxonID marks a taxon-page item. No identification or occurrence is inferred; originals retain it.',
    'http://eol.org/schema/media/thumbnailURL': 'A thumbnail would be a second media resource; adding it is a modelling decision. Originals retain it.',
    'http://eol.org/schema/agent/agentID': 'EOL agent identifiers point to records outside this archive and state no role; no agent is created.',
    'http://eol.org/schema/reference/referenceID': 'The pinned schema has no media-reference table; the value is not used as any relationship key.',
}
_EXACT_REFERENCE = {DCT + 'title': 'title', BIBO + 'pages': 'pages', BIBO + 'volume': 'volume', BIBO + 'edition': 'edition'}
_FULL_REFERENCE_NOTE = (' EOL states that full_reference makes the structured fields be ignored; decide whether structured '
                        'title/pages/volume/edition values are still copied when both are supplied.')
_REFERENCE_ALIASES = {
    DCT + 'identifier': ('referenceID', 'Target is dwc:referenceID. EOL defines the identifier of the referenced work; '
                                        'confirm the alias. No DOI or URI substitutes for it.'),
    'http://eol.org/schema/reference/publicationType': ('referenceType', 'Target is dwc:referenceType; confirm the EOL-namespace alias.'),
    'http://eol.org/schema/reference/full_reference': ('bibliographicCitation', 'Target is dcterms:bibliographicCitation; '
                                                                              'confirm the EOL-namespace alias.' + _FULL_REFERENCE_NOTE),
    BIBO + 'authorList': ('author', 'Target is dc:creator. bibo:authorList is an ordered list with unknown separators; confirm the alias.'),
    BIBO + 'editorList': ('editor', 'Target is bibo:editor (single property); the source is the ordered bibo:editorList.'),
}
_REFERENCE_PRESERVED = {
    DCT + 'created': 'Creation is not formal issuance; dcterms:created is never copied to issued.',
    BIBO + 'doi': 'There is no DOI field; a DOI is never substituted for referenceID.',
    BIBO + 'uri': 'There is no URI field; a URI is never substituted for referenceID.',
    BIBO + 'pageStart': 'There is no pageStart field. Composing pages from a range is a reformatting decision; originals retain it.',
    BIBO + 'pageEnd': 'There is no pageEnd field. Composing pages from a range is a reformatting decision; originals retain it.',
}
# Which registered source terms may supply each bibliographic-resource field.
_REFERENCE_SOURCES = {field: {term} for term, field in _EXACT_REFERENCE.items()}
_REFERENCE_SOURCES.update({field: {term} for term, (field, _) in _REFERENCE_ALIASES.items()})
_REFERENCE_SOURCES.update(publisher={DCT + 'publisher'}, publisherID={DCT + 'publisher'})


def _shape(values):
    return bool(values) and all(absolute_iri(v) for v in values), any(absolute_iri(v) for v in values)


def _split(values, literal, iri, alias_reason):
    all_iris, any_iris = _shape(values)
    if any_iris and not all_iris:
        return [], 'This column mixes identifiers and literals. Split it before mapping; originals retain every value.'
    return [iri if all_iris else literal], alias_reason


def _media_targets(term, values):
    if term in _SHARED_MEDIA_TERMS:
        return media_targets(term, values)
    if term == DCT + 'language':
        all_iris, any_iris = _shape(values)
        if all_iris:
            return ['media.languageIRI'], None
        if any_iris:
            return [], 'This column mixes identifiers and literals. Split it before mapping; originals retain every value.'
        return ['media.language'], 'EOL defines ISO 639 codes. Confirm the alias to the dc:language literal field.'
    if term == LEGACY_SUBTYPE:
        return _split(values, 'media.subtypeLiteral', 'media.subtypeIRI',
                      'Legacy audubon_core namespace, not ac:subtype. Confirm the alias.')
    if term == IPTC + 'CVterm':
        if _shape(values)[0]:
            return ['media.subjectCategoryIRI'], 'Iptc4xmpExt:CVterm is not ac:CVterm. Confirm the alias.'
        return [], 'Iptc4xmpExt:CVterm literals have no audited target; originals retain them.'
    return [], _MEDIA_PRESERVED.get(term)


def _reference_targets(term, values):
    if term in _EXACT_REFERENCE:
        return [f'bibliographic-resource.{_EXACT_REFERENCE[term]}'], None
    if term in _REFERENCE_ALIASES:
        field, reason = _REFERENCE_ALIASES[term]
        return [f'bibliographic-resource.{field}'], reason
    if term == DCT + 'publisher':
        return _split(values, 'bibliographic-resource.publisher', 'bibliographic-resource.publisherID',
                      'Target is dc:publisher (literal) or dwc:agentID (IRI). Confirm the alias; no agent is created.')
    return [], _REFERENCE_PRESERVED.get(term)


def eol_targets(row_type, term, values):
    """Return (targets, review_reason) for one whole source column of an EOL row type."""
    if row_type == EOL_MEDIA:
        return _media_targets(term, values)
    if row_type == EOL_REFERENCE:
        return _reference_targets(term, values)
    return [], None


def _filled(value):
    return isinstance(value, str) and value.strip() != ''


def eol_media_row_review(source):
    """Return a reason this media row cannot convert without an explicit row decision, or None."""
    reasons = []
    kind = (source.get(DCT + 'type') or '').strip().lower()
    if kind in TEXT_TYPES:
        reasons.append('This is a Text item (a taxon text account), not a media resource; preserve the row.')
    for term, label in ((DCT + 'identifier', 'dcterms:identifier'), (DCT + 'type', 'dcterms:type'),
                        (XMP + 'rights/UsageTerms', 'xmpRights:UsageTerms')):
        if not _filled(source.get(term)):
            reasons.append(f'The source requires {label}, but it is empty; no usage policy or media type is invented.')
    if _filled(source.get('http://rs.tdwg.org/dwc/terms/taxonID')) or (source.get(IPTC + 'CVterm') or '').strip().startswith(SPM_INFO_ITEMS):
        reasons.append('Taxon-page signals (taxonID or an SPMInfoItems subject) mean the item may depict the taxon generally, '
                       'not this occurrence or event. Confirm the subject link.')
    return ' '.join(reasons) or None


def _reference_values(source, mapped):
    if not isinstance(mapped, dict) or not isinstance(source, dict):
        raise ImportFailure('Source and mapped EOL reference values must be mappings.')
    values = {}
    for table, fields in mapped.items():
        if table != 'bibliographic-resource' or not isinstance(fields, dict):
            raise ImportFailure(f'{table} is not an audited EOL reference target table.')
        for field, value in fields.items():
            if field not in _REFERENCE_SOURCES:
                raise ImportFailure(f'bibliographic-resource.{field} is not an audited EOL reference target.')
            if value is None:
                value = ''
            if not isinstance(value, str):
                raise ImportFailure(f'bibliographic-resource.{field} must be text.')
            if not _filled(value):
                continue
            if value not in {source.get(term) for term in _REFERENCE_SOURCES[field]}:
                raise ImportFailure(f'bibliographic-resource.{field} does not equal its approved source value.')
            values[field] = value
    if 'publisher' in values and 'publisherID' in values:
        raise ImportFailure('One dcterms:publisher value cannot fill both publisher and publisherID.')
    return values


def emit_eol_records(family, source, mapped, subject_table, subject_key, record_key):
    if family == 'eol-media':
        raise ImportFailure('EOL media rows are emitted by the shared media path with an explicit subject choice.')
    if family != 'eol-reference':
        raise ImportFailure(f'Unsupported EOL family: {family!r}.')
    if subject_table not in REFERENCE_SUBJECTS:
        raise ImportFailure(f'An EOL reference cannot be linked to {subject_table!r}.')
    if not _filled(subject_key):
        raise ImportFailure(f'An EOL reference row does not resolve to a converted {subject_table}.')
    if not _filled(record_key):
        raise ImportFailure('An EOL reference row requires a deterministic record key.')
    values = _reference_values(source, mapped)
    if not _filled(source.get(DCT + 'identifier')):
        raise ImportFailure('EOL reference rows require a nonempty dcterms:identifier.')
    if not values:
        raise ImportFailure('An EOL reference row has no mapped bibliographic value; keep it in the originals.')
    # One record per source row; supplied identifiers never become keys and relationshipType is never inferred.
    return [('bibliographic-resource', {'reference_pk': record_key, **values}),
            (f'{subject_table}-reference', {'reference_fk': record_key, REFERENCE_SUBJECTS[subject_table]: subject_key})]
