"""Build offline header lookup from the audited registry-files.json snapshot.

Run in the backend container with the audit directory as the first argument.
Published latest definitions take precedence over sandbox and older versions.
Explicit meta.xml IRIs are never rewritten by this lookup.
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

from lxml import etree

root = Path(sys.argv[1])
terms = defaultdict(set)
definitions = defaultdict(list)
for item in json.loads((root / 'registry-files.json').read_text()):
    if item['kind'] != 'extension':
        continue
    xml = etree.parse(str(root / item['path'])).getroot()
    row_type = xml.get('rowType') or (xml.get('namespace') or '') + (xml.get('name') or '')
    columns = defaultdict(set)
    for prop in xml.iter():
        if not isinstance(prop.tag, str) or etree.QName(prop).localname != 'property':
            continue
        name = prop.get('name')
        iri = prop.get('qualName') or (prop.get('namespace') or '') + (name or '')
        if name and iri:
            terms[name].add(iri); columns[name].add(iri)
    for membership in item['registries']:
        if not membership['isLatest']:
            continue
        priority = 1 if membership['registry'] == 'production-extensions' else 0
        definitions[row_type].append((priority, membership['issued'] or '', item['url'], item['sha256'], columns))

row_types, provenance = {}, {}
for row_type, candidates in sorted(definitions.items()):
    priority, issued, url, checksum, columns = max(candidates, key=lambda item: item[:3])
    row_types[row_type] = {name: sorted(values) for name, values in sorted(columns.items())}
    provenance[row_type] = {'url': url, 'sha256': checksum, 'issued': issued, 'registry': 'production' if priority else 'sandbox'}
value = {'snapshot': '2026-10-02', 'terms': {name: sorted(values) for name, values in sorted(terms.items())},
         'row_types': row_types, 'definitions': provenance}
destination = Path(__file__).resolve().parents[1] / 'api/templates/dwca-conversion/registry.json'
destination.parent.mkdir(parents=True, exist_ok=True)
destination.write_text(json.dumps(value, indent=2) + '\n')
print(f'{len(terms)} term names; {len(row_types)} current row types')
