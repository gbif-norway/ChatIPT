"""Audited media column choices; identifiers never double as relationship keys."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from api.dwc_dp_specs import TABLE_SPECS

AC = 'http://rs.tdwg.org/ac/terms/'
DC = 'http://purl.org/dc/elements/1.1/'
DCT = 'http://purl.org/dc/terms/'
MEDIA_FAMILIES = {
    AC + 'Multimedia': 'media',
    'http://rs.gbif.org/terms/1.0/Multimedia': 'media',
    'http://rs.gbif.org/terms/1.0/Image': 'media',
}
MEDIA_SUBJECT_TERMS = {AC + 'associatedSpecimenReference', AC + 'associatedObservationReference',
                       'http://rs.tdwg.org/dwc/terms/preparations'}


def absolute_iri(value):
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9+.-]*:[^\s]+', value):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return bool(parsed.netloc) if parsed.scheme.lower() in {'http', 'https', 'ftp'} else True


def media_targets(term, values):
    """Return targets and a review explanation for an entire source column.

    Exact matches use the pinned field definitions. Explicit aliases always need
    approval. Mixed IRI/literal columns are preserved until the source is split.
    """
    if term == DCT + 'identifier':
        return ['media.mediaID'], None
    all_iris = bool(values) and all(absolute_iri(value) for value in values)
    any_iris = any(absolute_iri(value) for value in values)
    if term in {DCT + 'format', DCT + 'source', DCT + 'rights'} and values and not all_iris:
        literal = {DCT + 'format': 'media.format', DCT + 'source': 'provenance.source',
                   DCT + 'rights': 'usage-policy.rights'}[term]
        if any_iris:
            return [], 'This column mixes identifiers and literals. Split it before mapping; originals retain every value.'
        return [literal], 'The resource-valued source term contains literals. Confirm the explicit alias to the literal target field.'
    aliases = {
        DC + 'type': ['media.mediaType'],
        DCT + 'created': ['media.createDate'],
        DCT + 'creator': ['provenance.creatorID' if all_iris else 'provenance.creator'],
        AC + 'commenter': ['media.commenterID'],
        AC + 'reviewer': ['media.reviewerID'],
        AC + 'provider': ['provenance.providerID'],
        AC + 'metadataCreator': ['provenance.metadataCreatorID'],
        AC + 'metadataProvider': ['provenance.metadataProviderID'],
        AC + 'derivedFrom': ['media.derivedFromMediaID'],
    }
    if term in aliases:
        if term == DCT + 'creator' and any_iris and not all_iris:
            return [], 'Creator values mix agent identifiers and names; split the column before mapping.'
        target = aliases[term][0]
        if target.endswith('ID') and values and not all_iris:
            return [], 'Agent and derivation identifiers need absolute IRIs in this converter; names and unresolved local references remain in originals.'
        return aliases[term], 'Confirm this explicit alias between source and target terms. No agent or parent-media records are inferred.'
    # A generic identifier/agentID annotation occurs on several unrelated keys.
    # Such fields require role-specific source terms, never an arbitrary choice.
    matches = []
    for name in ('media', 'usage-policy', 'provenance'):
        for field in TABLE_SPECS[name].schema['fields']:
            if field.get('dcterms:isVersionOf') != term:
                continue
            if field['name'].endswith(('_pk', '_fk')) or term in {DCT + 'identifier', 'http://rs.tdwg.org/dwc/terms/agentID'}:
                continue
            matches.append(f"{name}.{field['name']}")
    if term == AC + 'furtherInformationURL':
        matches = ['media.furtherInformationURL']
    if len(matches) != 1:
        return [], None
    if matches[0].endswith('IRI') and values and not all_iris:
        return [], 'The target is an IRI field but some source values are not absolute IRIs. Originals retain the column.'
    return matches, None
