import csv
import io
import json
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from api.conversion_evidence import dependencies
from api.dwca_conversion import (DWC, _qualified_name, build_plan, convert,
                                validate_decisions)
from api.dwca_import import read_inputs, source_zip
from api.dwca_media import DCT
from api.dwca_review import conditional_defaults, effective_decisions, option_status
from api.dwc_dp_specs import create_dwc_dp_archive, validate_dwc_dp_archive


def specimen_archive(*, shared=False, missing_occurrence=False, recorded_ids=False, id_first=False):
    original_fields = ['occurrenceID', 'basisOfRecord', 'institutionCode', 'collectionCode', 'catalogNumber',
              'recordNumber', 'recordedBy', 'recordedByID', 'typeStatus', 'modified', 'preparations',
              'disposition', 'materialSampleID', 'eventDate', 'scientificName', 'occurrenceStatus']
    fields = list(original_fields)
    if id_first:
        fields.remove('recordedByID')
        fields.insert(fields.index('recordedBy'), 'recordedByID')
    rows = [
        ['urn:catalog:O:L:1', 'PreservedSpecimen', 'NHM', 'ZOO', '1001', 'R-1', 'Hagen, Yngvar',
         'https://orcid.org/0000-0001' if recorded_ids else '', 'Holotype of Aus bus', '2024-01-01', 'Pinned', 'In collection', 'urn:uuid:m1', '2020-05-01', 'Aus bus', 'present'],
        ['urn:catalog:O:L:2', 'PreservedSpecimen', 'NHM', 'ZOO', '1002', '', 'Collett, Robert',
         'https://orcid.org/0000-0002' if recorded_ids else '', '', '2024-01-02', 'Pinned', 'In collection', 'urn:uuid:m2', '2020-05-02', 'Aus bus', 'present'],
        ['urn:catalog:O:L:3', 'MaterialSample', 'NHM', 'ZOO', '1003', 'R-3', 'Kjernslie, O.L.',
         'https://orcid.org/0000-0003' if recorded_ids else '', '', '2024-01-03', 'Tissue', 'In collection', 'urn:uuid:m3', '2020-05-03', 'Aus bus', 'present'],
    ]
    if missing_occurrence:
        # A placeholder occurrenceID: the loose core still joins on it, but it identifies nothing.
        rows[2][0] = 'NA'
    if shared:
        rows[1][12] = rows[0][12]
    if id_first:
        rows = [[row[original_fields.index(name)] for name in fields] for row in rows]
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(fields)
    writer.writerows(rows)
    return read_inputs([('occurrence.csv', stream.getvalue().encode())])


class ConversionQuestionTests(SimpleTestCase):
    def test_material_details_and_agent_columns_follow_one_material_choice(self):
        archive = specimen_archive(recorded_ids=True)
        plan = build_plan(archive)
        column_by_term = {column['term'].rsplit('/', 1)[-1]: column for column in plan['columns']}
        automatic_ids = {item['id'] for item in plan['automatic_choices']}
        asked_ids = {item['id'] for item in plan['issues']}
        for name in ('institutionCode', 'collectionCode', 'catalogNumber', 'recordNumber', 'preparations',
                     'disposition', 'modified', 'recordedBy', 'recordedByID', 'typeStatus'):
            column = column_by_term[name]
            self.assertNotIn(column['id'], asked_ids)
        self.assertTrue(all(column_by_term[name]['follows'] == 'material:0' for name in
                            ('institutionCode', 'collectionCode', 'catalogNumber', 'recordNumber',
                             'preparations', 'disposition', 'modified', 'materialSampleID')))
        self.assertIn(column_by_term['recordedBy']['id'], automatic_ids)
        self.assertIn(column_by_term['recordedByID']['id'], automatic_ids)
        self.assertIn(column_by_term['typeStatus']['id'], automatic_ids)
        material = next(issue for issue in plan['issues'] if issue['id'] == 'material:0')
        self.assertEqual(material['followers'], [column['id'] for column in plan['columns'] if column['term'].rsplit('/', 1)[-1] in
            ('institutionCode', 'collectionCode', 'catalogNumber', 'recordNumber', 'preparations', 'disposition', 'modified', 'materialSampleID')])
        self.assertIn('If yes, these details are stored on the specimen records:', material['reason'])

    def test_per_row_material_routes_and_export_validate(self):
        archive = specimen_archive(recorded_ids=True)
        plan = build_plan(archive)
        decisions = {issue['id']: issue['options'][0]['value'] for issue in plan['issues']}
        decisions['material:0'] = 'per_row'
        validate_decisions(plan, decisions)
        effective = effective_decisions(plan, decisions)
        frames, report = convert(archive, plan, decisions)
        self.assertTrue(report['validation']['valid'])
        self.assertEqual(frames['material']['collectedBy'].tolist(), ['Hagen, Yngvar', 'Collett, Robert', 'Kjernslie, O.L.'])
        self.assertEqual(frames['material']['collectedByID'].tolist(), [
            'https://orcid.org/0000-0001', 'https://orcid.org/0000-0002', 'https://orcid.org/0000-0003'])
        self.assertEqual(frames['material']['typeStatus'].tolist(), ['Holotype of Aus bus', '', ''])
        self.assertEqual(frames['material']['catalogNumber'].tolist(), ['1001', '1002', '1003'])
        self.assertEqual(frames['material']['collectorNumber'].tolist(), ['R-1', '', 'R-3'])
        self.assertTrue('recordedBy' not in frames['occurrence'] or not frames['occurrence']['recordedBy'].fillna('').any())
        self.assertEqual(frames['material-agent-role']['agentRole'].tolist(), ['collectedBy'] * 3)
        self.assertNotIn('recordedBy', set(frames.get('occurrence-agent-role', {}).get('agentRole', [])))
        self.assertEqual(frames['material']['evidenceForOccurrenceID'].tolist(), [
            'urn:catalog:O:L:1', 'urn:catalog:O:L:2', 'urn:catalog:O:L:3'])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'specimens.tar.gz'
            create_dwc_dp_archive(output, frames, 'Specimens', 'Test fixture', include_eml=False,
                additional_files=[('source-originals.zip', source_zip(archive.files)),
                                  ('conversion-report.json', json.dumps(report).encode())],
                declare_additional_resources=True)
            self.assertTrue(validate_dwc_dp_archive(output, require_eml=False)['valid'])
        self.assertEqual(effective[column_by_term_id(plan, 'recordedBy')], 'material.collectedBy')

    def test_preserve_keeps_material_columns_in_originals_and_agents_on_occurrence(self):
        archive = specimen_archive(recorded_ids=True)
        plan = build_plan(archive)
        decisions = {issue['id']: issue['options'][0]['value'] for issue in plan['issues']}
        decisions['material:0'] = 'preserve'
        frames, report = convert(archive, plan, decisions)
        self.assertTrue(report['validation']['valid'])
        self.assertEqual(frames['occurrence']['recordedBy'].tolist(), ['Hagen, Yngvar', 'Collett, Robert', 'Kjernslie, O.L.'])
        self.assertEqual(frames['occurrence']['recordedByID'].tolist(), [
            'https://orcid.org/0000-0001', 'https://orcid.org/0000-0002', 'https://orcid.org/0000-0003'])
        self.assertEqual(frames['identification']['typeStatus'].tolist(), ['Holotype of Aus bus'])
        self.assertNotIn('material', frames)

    def test_conditional_defaults_are_fixed_point_resolved_and_explicit_choices_win(self):
        archive = specimen_archive(recorded_ids=True, id_first=True)
        plan = build_plan(archive)
        decisions = {'material:0': 'per_row'}
        resolved = conditional_defaults(plan, decisions)
        by_term = {column['term'].rsplit('/', 1)[-1]: column for column in plan['columns']}
        self.assertEqual(resolved[by_term['recordedBy']['id']]['value'], 'material.collectedBy')
        self.assertEqual(resolved[by_term['recordedByID']['id']]['value'], 'material.collectedByID')
        self.assertIn('who collected the specimen', resolved[by_term['recordedBy']['id']]['reason'])
        self.assertEqual(effective_decisions(plan, decisions)[by_term['recordedByID']['id']], 'material.collectedByID')
        fallback = conditional_defaults(plan, {'material:0': 'preserve'})
        self.assertEqual(fallback[by_term['recordedBy']['id']], {'value': 'occurrence.recordedBy', 'reason': None})
        explicit = {'material:0': 'per_row', by_term['recordedBy']['id']: 'event.eventConductedBy'}
        self.assertEqual(effective_decisions(plan, explicit)[by_term['recordedByID']['id']], 'event.eventConductedByID')
        explicit[by_term['recordedByID']['id']] = 'occurrence.recordedByID'
        self.assertEqual(effective_decisions(plan, explicit)[by_term['recordedByID']['id']], 'occurrence.recordedByID')

    def test_missing_occurrence_id_prevents_collector_link_default(self):
        plan = build_plan(specimen_archive(missing_occurrence=True))
        column_id = column_by_term_id(plan, 'recordedBy')
        branch = next(branch for branch in next(column for column in plan['columns'] if column['id'] == column_id)['default_when']
                      if branch['value'] == 'occurrence.recordedBy')
        self.assertIn('some rows have no usable occurrenceID', branch['reason'])
        defaults = conditional_defaults(plan, {'material:0': 'per_row'})
        self.assertEqual(defaults[column_id]['value'], 'occurrence.recordedBy')

    def test_by_id_availability_recomputes_conditional_column_targets(self):
        source_plan = build_plan(specimen_archive(shared=True))
        recorded = next(column for column in source_plan['columns'] if column['term'] == DWC + 'recordedBy')
        combined = next(branch for branch in recorded['default_when'] if branch['value'] == 'occurrence.recordedBy')
        self.assertIn('combined by identifier', combined['reason'])
        plan = {'tables': [{'core': True}], 'columns': [
            {'id': 'column:0:0', 'table': 0, 'default': 'occurrence.recordedBy', 'default_when': [
                {'value': 'material.collectedBy', 'when': [{'type': 'decision_in', 'id': 'material:0', 'values': ['per_row']}]},
                {'value': 'occurrence.recordedBy', 'when': [{'type': 'decision_in', 'id': 'material:0', 'values': ['by_id']}]}]}],
            'issues': [{'id': 'material:0', 'table': 0, 'kind': 'material-identity',
                        'options': [{'value': 'preserve'}, {'value': 'per_row'}, {'value': 'by_id'}]}],
            'automatic_choices': [], 'requirements': {'material:0': {'by_id': [
                {'reason': 'recordedBy would conflict if copied to material', 'conditions': [
                    {'type': 'target_not_in', 'column': 'column:0:0', 'targets': ['material.collectedBy']}]}]}}}
        statuses = option_status(plan, {'material:0': 'per_row'})
        self.assertTrue(statuses['material:0']['by_id']['available'])

    def test_dependencies_include_ask_when_and_conditional_material_answer(self):
        plan = {'tables': [{'core': True}], 'columns': [
            {'id': 'column:0:0', 'table': 0, 'default': 'material.materialEntityID', 'default_when': [
                {'value': 'material.materialEntityID', 'when': [{'type': 'decision_in', 'id': 'material:0', 'values': ['per_row']}]}]},
            {'id': 'column:0:1', 'table': 0, 'default': 'material.materialEntityID'}],
            'issues': [{'id': 'material:0', 'table': 0, 'kind': 'material-identity', 'options': []},
                       {'id': 'column:0:1', 'table': 0, 'kind': 'column-mapping', 'ask_when': [
                           {'type': 'decision_in', 'id': 'material:0', 'values': ['per_row']}], 'options': []}],
            'automatic_choices': [{'id': 'column:0:0', 'table': 0, 'kind': 'agent-role', 'options': []}], 'requirements': {}}
        self.assertIn('material:0', dependencies(plan, 'column:0:1'))
        self.assertIn('material:0', dependencies(plan, 'column:0:0'))

    def test_ask_when_hides_issue_until_material_records_are_created(self):
        plan = {'columns': [], 'tables': [{'core': True}], 'automatic_choices': [],
                'issues': [{'id': 'material:0', 'options': [{'value': 'preserve'}, {'value': 'per_row'}]},
                           {'id': 'column:0:1', 'table': 0, 'options': [{'value': 'preserve'}],
                            'ask_when': [{'type': 'decision_in', 'id': 'material:0', 'values': ['per_row', 'by_id']}]}],
                'requirements': {}}
        self.assertEqual(validate_decisions(plan, {'material:0': 'preserve'}, require_complete=False), [])
        self.assertEqual(validate_decisions(plan, {'material:0': 'per_row'}, require_complete=False), ['column:0:1'])

    def test_scientific_name_only_offers_occurrence_mapping(self):
        plan = build_plan(read_inputs([('occurrence.csv', b'occurrenceID,scientificName,occurrenceStatus\no1,Aus bus,present\n')]))
        column = next(item for item in plan['columns'] if item['term'] == DWC + 'scientificName')
        self.assertEqual([option['value'] for option in column['options']], ['occurrence.scientificName', 'preserve'])

    def test_single_target_media_aliases_are_automatic(self):
        archive = read_inputs([
            ('occurrence.csv', b'occurrenceID,occurrenceStatus\no1,present\n'),
            ('multimedia.csv', ('occurrenceID,' + ','.join((DCT + 'format', DCT + 'created', DCT + 'creator')) +
                                '\no1,JPEG,2024-01-01,Photographer Name\n').encode())])
        plan = build_plan(archive)
        columns = {column['term']: column for column in plan['columns']}
        automatic = {choice['id']: choice for choice in plan['automatic_choices']}
        for term in (DCT + 'format', DCT + 'created', DCT + 'creator'):
            column = columns[term]
            self.assertFalse(column['review'])
            self.assertEqual(automatic[column['id']]['family'], 'media')
            self.assertIn('exactly as written', automatic[column['id']]['reason'])

    def test_filius_suffix_is_not_mistaken_for_forma_rank(self):
        self.assertEqual(_qualified_name('Aus bus L. f.', 'cf.'), 'Aus cf. bus L. f.')
        self.assertEqual(_qualified_name('Aus bus Hook. f.', 'cf.'), 'Aus cf. bus Hook. f.')
        self.assertIsNone(_qualified_name('Aus bus L. f. cus', 'cf.'))
        # A real forma keeps today's placement: before the final epithet and its rank marker.
        self.assertEqual(_qualified_name('Aus bus f. cus', 'cf.'), 'Aus bus cf. f. cus')

    def test_plan_id_is_deterministic(self):
        archive = specimen_archive(recorded_ids=True)
        self.assertEqual(build_plan(archive)['id'], build_plan(archive)['id'])


def column_by_term_id(plan, short_name):
    return next(column['id'] for column in plan['columns'] if column['term'].rsplit('/', 1)[-1] == short_name)
