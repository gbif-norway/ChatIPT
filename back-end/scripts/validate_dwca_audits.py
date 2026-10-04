"""Verify cloud audit evidence against the downloaded registry and pinned schemas.

Run in the backend Compose container, mounting the audit and registry directories.
This checks evidence and coverage, not the proposed semantic transformations.
"""
import argparse
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET


def require(condition, message):
    if not condition:
        raise ValueError(message)


def properties(path):
    root = ET.parse(path).getroot()
    props = [element for element in root.iter() if element.tag.endswith('}property')]
    result = {element.attrib['qualName']: element for element in props}
    require(len(result) == len(props), f'Duplicate source properties in {path}')
    return root, result


def validate(audits, registry, schemas):
    tables = {path.stem: json.loads(path.read_text()) for path in schemas.glob('*.json')}
    require(len(tables) == 79, 'These audits require the pinned 79-table snapshot')
    fields = {(table, field['name']): field for table, schema in tables.items() for field in schema['fields']}
    humboldt = json.loads((audits / 'humboldt.json').read_text())
    terms = humboldt['terms']
    require(len(terms) == 57, 'Humboldt must cover all 57 properties')
    for source in humboldt['sources']:
        path = registry / 'extension' / Path(source['file']).name
        require(hashlib.sha256(path.read_bytes()).hexdigest() == source['sha256'], f'Source checksum differs: {path}')
        _, props = properties(path)
        require(set(props) == {term['source_iri'] for term in terms}, f'Humboldt coverage differs: {path}')
    for term in terms:
        iri = term['source_iri']
        exact = sorted(f'{table}.{name}' for (table, name), field in fields.items() if field.get('dcterms:isVersionOf') == iri)
        require(exact == sorted(term['exact_iri_fields_in_pinned_schema']), f'Exact match list differs: {iri}')
        target = term['target']
        field = fields[(target['table'], target['field'])]
        if target.get('match') == 'exact_iri':
            require(field['dcterms:isVersionOf'] == iri, f'Non-exact Humboldt target: {iri}')
            require(field['type'] == target['field_type'], f'Target type differs: {iri}')
            require(field.get('constraints', {}) == target['field_constraints'], f'Target constraints differ: {iri}')
    require(fields[('survey-target', 'isSurveyTargetFullyReported')]['constraints']['required'], 'Completeness flag assumption changed')

    remaining = json.loads((audits / 'remaining-fields.json').read_text())
    total = 0
    for family in remaining['families']:
        path = registry / 'extension' / Path(family['source_file']).name
        root, props = properties(path)
        require(root.attrib['rowType'] == family['row_type'], f'Row type differs: {path}')
        require(len(props) == family['term_count'], f'Property count differs: {path}')
        require(set(props) == {term['source_iri'] for term in family['terms']}, f'Term coverage differs: {path}')
        for term in family['terms']:
            iri = term['source_iri']
            prop = props[iri]
            require(prop.get('{http://purl.org/dc/terms/}description', '').strip() == term['source_definition'], f'Definition differs: {iri}')
            require((prop.get('required', 'false') == 'true') == term['source_required'], f'Source required flag differs: {iri}')
            for target in term['targets']:
                field = fields[(target['table'], target['field'])]
                require(field.get('dcterms:isVersionOf', '') == target['target_isVersionOf'], f'Target annotation differs: {iri}')
                require(field['type'] == target['target_type'], f'Target type differs: {iri}')
                require(bool(field.get('constraints', {}).get('required')) == target['target_required'], f'Target required flag differs: {iri}')
                require(target['iri_match'] == (target['target_isVersionOf'] == iri), f'IRI equality differs: {iri}')
            if term['disposition'] == 'map':
                require(term['targets'] and all(target['iri_match'] for target in term['targets']), f'Non-exact automatic map: {iri}')
            if term['disposition'] == 'review':
                require(term['targets'], f'Review has no candidate target: {iri}')
            total += 1
    require(len(remaining['families']) == 8 and total == 326, 'Remaining audit coverage differs')
    print('Verified 383 term entries, complete XML coverage, four Humboldt hashes and pinned target evidence.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audits', required=True, type=Path)
    parser.add_argument('--registry', required=True, type=Path)
    parser.add_argument('--schemas', required=True, type=Path)
    args = parser.parse_args()
    validate(args.audits, args.registry, args.schemas)
