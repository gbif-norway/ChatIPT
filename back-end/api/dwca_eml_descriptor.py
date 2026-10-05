"""Extract source EML metadata that maps safely to a Data Package descriptor.

Source EML remains the authoritative copy. This module only promotes fields
whose meanings align with the Frictionless Data Package descriptor; unmappable
rights, citations and coverage remain available as source metadata for reporting.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from lxml import etree


_LICENSE_URLS = {
    'https://creativecommons.org/publicdomain/zero/1.0/': ('cc0-1.0', 'CC0 1.0'),
    'https://creativecommons.org/licenses/by/4.0/': ('cc-by-4.0', 'CC BY 4.0'),
    'https://creativecommons.org/licenses/by-sa/4.0/': ('cc-by-sa-4.0', 'CC BY-SA 4.0'),
    'https://creativecommons.org/licenses/by/3.0/': ('cc-by-3.0', 'CC BY 3.0'),
    'https://creativecommons.org/licenses/by-sa/3.0/': ('cc-by-sa-3.0', 'CC BY-SA 3.0'),
    'https://opendatacommons.org/licenses/odbl/1-0/': ('odc-odbl', 'Open Data Commons Open Database License 1.0'),
    'https://opendatacommons.org/licenses/pddl/1-0/': ('odc-pddl', 'Open Data Commons Public Domain Dedication and License 1.0'),
}
_CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')


def _parse(content):
    if isinstance(content, str):
        content = content.encode('utf-8')
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)
    try:
        root = etree.fromstring(content, parser=parser)
    except etree.XMLSyntaxError as error:
        raise ValueError('Source EML is not well-formed XML.') from error
    datasets = root.xpath('.//*[local-name()="dataset"]')
    if etree.QName(root).localname == 'dataset':
        datasets.insert(0, root)
    if len(datasets) != 1:
        raise ValueError('Source EML must contain exactly one dataset element.')
    return datasets[0]


def _clean(element):
    if element is None:
        return ''
    return ' '.join(_CONTROL.sub('', ' '.join(element.itertext())).split())


def _all(parent, path):
    return parent.xpath('./' + '/'.join(f'*[local-name()="{part}"]' for part in path.split('/')))


def _first_text(parent, path):
    return next((_clean(element) for element in _all(parent, path) if _clean(element)), '')


def _agents(dataset):
    contributors = []
    for element in _all(dataset, 'creator'):
        role = 'author'
        contributor = _agent(element, role)
        if contributor:
            contributors.append(contributor)
    for element in _all(dataset, 'associatedParty'):
        contributor = _agent(element, _first_text(element, 'role') or 'contributor')
        if contributor:
            contributors.append(contributor)
    # Deterministic de-duplication; preserve source ordering for distinct roles.
    seen, result = set(), []
    for item in contributors:
        key = tuple(sorted(item.items()))
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _agent(element, role):
    name_parts = [_first_text(element, f'individualName/{part}') for part in ('givenName', 'surName')]
    title = ' '.join(part for part in name_parts if part) or _first_text(element, 'organizationName')
    if not title:
        return None
    result = {'title': title}
    email = _first_text(element, 'electronicMailAddress')
    if email and re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
        result['email'] = email
    org = _first_text(element, 'organizationName')
    if org and org != title:
        result['organization'] = org
    if role:
        result['role'] = role
    return result


def _license_url(raw):
    """Canonicalize a direct, recognized license URL or its legalcode link."""
    try:
        parsed = urlsplit(raw.strip())
    except ValueError:
        return None
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.query or parsed.fragment:
        return None
    path = parsed.path.removesuffix('/legalcode').rstrip('/') + '/'
    candidate = f'https://{parsed.netloc.lower()}{path}'
    return candidate if candidate in _LICENSE_URLS else None


def extract_eml_descriptor_metadata(content):
    """Return ``{descriptor, source_metadata, warnings}`` from source EML 2.1.1.

    Descriptor keys are Frictionless Data Package metadata: ``licenses``,
    ``contributors`` and ``keywords``. Only an exact whitelisted license URI
    found as the entire intellectualRights text becomes a license assertion.
    Free-text rights, citation and coverage are returned under
    ``source_metadata`` for retention/reporting and are never guessed into
    descriptor fields. Title and abstract are excluded because the conversion
    workflow already handles them with explicit user/EML precedence.
    """
    dataset = _parse(content)
    descriptor = {}
    contributors = _agents(dataset)
    if contributors:
        descriptor['contributors'] = contributors

    keywords = []
    for element in _all(dataset, 'keywordSet/keyword'):
        value = _clean(element)
        if value and value not in keywords:
            keywords.append(value)
    if keywords:
        descriptor['keywords'] = keywords

    rights_elements = _all(dataset, 'intellectualRights')
    rights = '\n\n'.join(filter(None, (_clean(element) for element in rights_elements)))
    links = list(dict.fromkeys(url for element in rights_elements
                               for url in element.xpath('.//*[local-name()="ulink"]/@url')))
    license_url = _license_url(rights) if not links else _license_url(links[0]) if len(links) == 1 else None
    license_value = _LICENSE_URLS.get(license_url)
    if license_value:
        name, title = license_value
        descriptor['licenses'] = [{'name': name, 'path': license_url, 'title': title}]

    citation = '\n\n'.join(filter(None, (_clean(element) for element in _all(dataset, 'citation'))))
    coverage = {}
    geographic = []
    for item in _all(dataset, 'coverage/geographicCoverage'):
        desc = _first_text(item, 'geographicDescription')
        bounds = _all(item, 'boundingCoordinates')
        parts = [desc] if desc else []
        if bounds:
            box = bounds[0]
            values = {key: _first_text(box, key) for key in (
                'westBoundingCoordinate', 'eastBoundingCoordinate',
                'northBoundingCoordinate', 'southBoundingCoordinate')}
            if any(values.values()):
                parts.append(', '.join(f'{key.removesuffix("BoundingCoordinate")}={value}'
                                       for key, value in values.items() if value))
        if parts:
            geographic.append('; '.join(parts))
    temporal = []
    for item in _all(dataset, 'coverage/temporalCoverage'):
        value = _first_text(item, 'singleDateTime/calendarDate')
        if not value:
            begin = _first_text(item, 'rangeOfDates/beginDate/calendarDate')
            end = _first_text(item, 'rangeOfDates/endDate/calendarDate')
            value = '/'.join(part for part in (begin, end) if part)
        if value:
            temporal.append(value)
    taxonomic = []
    for item in _all(dataset, 'coverage/taxonomicCoverage/taxonomicClassification'):
        name = _first_text(item, 'taxonRankValue')
        rank = _first_text(item, 'taxonRankName')
        if name:
            taxonomic.append(f'{rank}: {name}' if rank else name)
    general_taxonomic = _first_text(dataset, 'coverage/taxonomicCoverage/generalTaxonomicCoverage')
    if general_taxonomic:
        taxonomic.insert(0, general_taxonomic)
    if geographic:
        coverage['geographic'] = geographic
    if temporal:
        coverage['temporal'] = temporal
    if taxonomic:
        coverage['taxonomic'] = taxonomic

    source_metadata = {}
    if rights and not license_value:
        source_metadata['intellectual_rights'] = rights
    if citation:
        source_metadata['citation'] = citation
    if coverage:
        source_metadata['coverage'] = coverage
    return {
        'descriptor': descriptor,
        'source_metadata': source_metadata,
        'warnings': ([{'field': 'intellectualRights', 'reason': 'Free-text rights were retained; they do not identify an unambiguous standard license.'}]
                     if rights and not license_value else []),
    }
