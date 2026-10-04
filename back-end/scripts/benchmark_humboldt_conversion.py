"""Independent offline verification, executed in the backend Compose service."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tarfile
import zipfile
import io

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'app.settings')
import django
django.setup()

from api.dwca_import import DWC, read_inputs, source_zip
from api.dwca_conversion import RULE_VERSION, build_plan, convert
from api.dwc_dp_specs import create_dwc_dp_archive, validate_dwc_dp_archive

parser = argparse.ArgumentParser(description='Offline verification of the pinned public dry-grassland archive; no model calls or database writes.')
parser.add_argument('--source-zip', type=Path, required=True)
parser.add_argument('--output-dir', type=Path, required=True)
args = parser.parse_args()
directory = args.output_dir
directory.mkdir(parents=True, exist_ok=True)
path = args.source_zip
raw = path.read_bytes()
expected_sha = 'd45be8017a7f87234ce3b9680f13b39a45aba3725613aff808a985f1050abb64'
if hashlib.sha256(raw).hexdigest() != expected_sha:
    raise SystemExit('Source checksum differs from the audited archive; re-audit it before running this benchmark.')
archive = read_inputs([(path.name, path.read_bytes())])
plan = build_plan(archive)
decisions = {issue['id']: issue['options'][0]['value'] for issue in plan['issues']}
frames, report = convert(archive, plan, decisions)
assert report['validation']['valid'], report['validation']
core = next(table for table in archive.tables if table.is_core)
event_index = core.terms.index(DWC + 'eventID')
parent_index = core.terms.index(DWC + 'parentEventID')
events = frames['event'].fillna('').to_dict('records')
assert len(events) == len(core.rows) == 390
by_identifier = {row['eventID']: row for row in events}
assert len(by_identifier) == len(events)
expected = {row[event_index]: row[parent_index] for row in core.rows if row[parent_index]}
assert len(expected) == 389
for child, parent in expected.items():
    assert by_identifier[child].get('parentEvent_fk') == by_identifier[parent]['event_pk'], child
assert sum(bool(row.get('parentEvent_fk')) for row in events) == len(expected)
identifier_by_join = {join: row[event_index] for row, join in zip(core.rows, core.ids)}
lineage_checks = {}
for name in ('occurrence', 'survey'):
    rows = {row[next(field for field in row if field.endswith('_pk'))]: row for row in frames[name].to_dict('records')}
    checked = set()
    for entry in report['row_crosswalk']:
        if entry['target_table'] != name:
            continue
        key = next(iter(entry['key'].values()))
        expected_event = by_identifier[identifier_by_join[entry['archive_join_id']]]['event_pk']
        assert rows[key]['event_fk'] == expected_event, (name, key)
        checked.add(key)
    assert len(checked) == len(rows)
    lineage_checks[name] = len(checked)
direct_survey_cells = 0
for entry in report['row_crosswalk']:
    if entry['target_table'] != 'survey':
        continue
    source_table = archive.tables[entry['source_table_index']]
    source_row = source_table.rows[entry['source_row'] - 1]
    survey_key = entry['key']['survey_pk']
    target_row = frames['survey'].set_index('survey_pk').loc[survey_key]
    for column in plan['columns']:
        target = decisions.get(column['id'], column['default'])
        if column['table'] != entry['source_table_index'] or not target.startswith('survey.'):
            continue
        value = source_row[column['column']]
        if value:
            assert target_row[target.split('.', 1)[1]] == value
            direct_survey_cells += 1
originals = source_zip(archive.files)
with zipfile.ZipFile(io.BytesIO(originals)) as preserved:
    assert set(preserved.namelist()) == set(archive.files)
    for name, content in archive.files.items():
        assert preserved.read(name) == content
again_plan = build_plan(archive)
again_frames, again_report = convert(archive, again_plan, decisions)
assert again_plan['id'] == plan['id']
assert set(again_frames) == set(frames)
for name, frame in frames.items():
    assert frame.to_csv(index=False) == again_frames[name].to_csv(index=False), name
output = directory / 'vegetation-converted.tar.gz'
create_dwc_dp_archive(output, frames, title='Independent Humboldt conversion benchmark',
    description='Explicit simulated choices for a public nested survey; not publisher approval.',
    include_eml=False, additional_files=[('source-originals.zip', originals), ('uploaded-archive.zip', raw),
    ('conversion-report.json', json.dumps(report).encode())], declare_additional_resources=True)
validation = validate_dwc_dp_archive(output, require_eml=False)
assert validation['valid'], validation
with tarfile.open(output, 'r:gz') as package:
    entry = next(member for member in package.getmembers() if member.name.endswith('source-originals.zip'))
    assert package.extractfile(entry).read() == originals
    upload = next(member for member in package.getmembers() if member.name.endswith('uploaded-archive.zip'))
    assert package.extractfile(upload).read() == raw
evidence = {'rule_version': RULE_VERSION, 'source':{'url':'https://cloud.gbif.org/eca/archive.do?r=dry_grasslands_palpurina_phdthesis', 'doi':'10.15468/pkx4tg', 'bytes':len(raw), 'sha256':expected_sha},
    'simulated_review': True, 'review_issues':len(plan['issues']), 'decisions':decisions,
    'automatic_choices': {item['id']: item['default'] for item in plan.get('automatic_choices', [])},
    'conversion_notices': len(plan.get('warnings', [])),
    'resources':{name:len(frame) for name, frame in frames.items()},
    'parent_links_verified':len(expected), 'direct_survey_cells_verified':direct_survey_cells, 'occurrence_and_survey_links_verified':lineage_checks,
    'source_originals_identical':True, 'uploaded_zip_identical':True,'repeated_plan_and_csv_identical':True,
    'validation':validation, 'withheld_values':len(report.get('withheld_values',[])),
    'scientific_hierarchy': plan.get('scientific_hierarchy'),
    'retained_populated_columns':sum(column['disposition']=='retained-unmapped' and bool(column['nonempty']) for column in report['columns'])}
(directory / 'vegetation-benchmark.json').write_text(json.dumps(evidence,indent=2))
print(json.dumps({key:value for key,value in evidence.items() if key not in {'decisions','source'}},indent=2))
