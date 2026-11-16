import io
import os
import zipfile
from unittest.mock import patch
from xml.etree import ElementTree as ET

import pandas as pd
from django.test import SimpleTestCase, TestCase

from api.agent_tools import UploadDwCA
from api.dna_projection import describe, dna_derived_data
from api.dwc_specs import DarwinCoreCoreType, DarwinCoreExtensionType
from api.models import Agent, Dataset, Table, Task


DNA_ROW_TYPE = 'http://rs.gbif.org/terms/1.0/DNADerivedData'
DNA_FILE = 'dna_derived_data_2024-07-11.txt'
PROTOCOL = {
    'molecularProtocol_pk': 'p12s', 'target_gene': '12S rRNA', 'pcr_primer_forward': 'GTCGGTAAAACTCGTGCCAGC',
    'samp_collect_device': 'swab', 'associated_resource': 'https://example.org/sop', 'assayType': 'metabarcoding',
    'sequence': 'NNNN',
}


def analysis(key, sequence, material='m1', event='e1'):
    return {'nucleotideAnalysis_pk': key, 'event_fk': event, 'materialEntity_fk': material,
            'nucleotideSequence_fk': sequence, 'molecularProtocol_fk': 'p12s', 'readCount': 120}


def metabarcoding():
    """One faecal sample (material m1, evidence for its host occurrence) with two DNA detections."""
    return {
        'event': pd.DataFrame([{'event_pk': 'e1', 'eventID': 'EV1'}]),
        'material': pd.DataFrame([{'materialEntity_pk': 'm1', 'collectionEvent_fk': 'e1', 'evidenceForOccurrence_fk': 'o-host'}]),
        'occurrence': pd.DataFrame([
            {'occurrence_pk': 'o-host', 'occurrenceID': 'HOST', 'event_fk': 'e1'},
            {'occurrence_pk': 'o-a', 'occurrenceID': 'S1_ESV_1', 'event_fk': 'e1'},
            {'occurrence_pk': 'o-b', 'occurrenceID': 'S1_ESV_2', 'event_fk': 'e1'},
        ]),
        'nucleotide-sequence': pd.DataFrame([{'nucleotideSequence_pk': 's1', 'sequence': 'ACGTAC'},
                                             {'nucleotideSequence_pk': 's2', 'sequence': 'TTGACC'}]),
        'molecular-protocol': pd.DataFrame([PROTOCOL]),
        'nucleotide-analysis': pd.DataFrame([analysis('a1', 's1'), analysis('a2', 's2')]),
        'identification': pd.DataFrame([
            {'identification_pk': 'i1', 'occurrence_fk': 'o-a', 'nucleotideAnalysis_fk': 'a1'},
            {'identification_pk': 'i1-field', 'occurrence_fk': 'o-a', 'nucleotideAnalysis_fk': 'a1'},
            {'identification_pk': 'i2', 'occurrence_fk': 'o-b', 'nucleotideAnalysis_fk': 'a2'},
        ]),
    }


CORE = {'HOST', 'S1_ESV_1', 'S1_ESV_2'}


class DnaDerivedDataTests(SimpleTestCase):
    def test_each_detection_gets_the_analysis_it_was_identified_from(self):
        rows, report = dna_derived_data(metabarcoding(), 'occurrence', CORE)

        self.assertEqual(rows['_coreid'].tolist(), ['S1_ESV_1', 'S1_ESV_2'])
        self.assertEqual(rows['DNA_sequence'].tolist(), ['ACGTAC', 'TTGACC'])
        self.assertEqual(rows['target_gene'].tolist(), ['12S rRNA'] * 2)
        self.assertEqual(rows['pcr_primer_forward'].tolist(), ['GTCGGTAAAACTCGTGCCAGC'] * 2)
        # Renamed by IRI, not by field name.
        self.assertEqual(rows['samp_collec_device'].tolist(), ['swab'] * 2)
        self.assertEqual(rows['url'].tolist(), ['https://example.org/sop'] * 2)
        self.assertNotIn('assayType', rows)
        self.assertEqual((report['rows'], report['core_records'], report['not_projected']), (2, 2, {}))
        self.assertEqual(report['skipped_protocol_sequences'], 1)
        self.assertEqual(report['kept_in_dwc_dp_only'],
                         {'nucleotide-analysis.readCount': 2, 'molecular-protocol.assayType': 1})

    def test_specimen_analyses_follow_the_single_evidence_material(self):
        resources = metabarcoding()
        del resources['identification']

        rows, report = dna_derived_data(resources, 'occurrence', CORE)

        self.assertEqual(rows['_coreid'].tolist(), ['HOST', 'HOST'])
        self.assertEqual(rows['DNA_sequence'].tolist(), ['ACGTAC', 'TTGACC'])
        self.assertEqual(report['not_projected'], {})

    def test_an_explicit_link_to_a_missing_core_record_never_falls_back_to_the_material(self):
        rows, report = dna_derived_data(metabarcoding(), 'occurrence', {'HOST', 'S1_ESV_2'})

        self.assertEqual(rows['_coreid'].tolist(), ['S1_ESV_2'])
        self.assertEqual(report['not_projected'], {'not_in_core': 1})

    def test_links_to_missing_core_records_are_reported_beside_projected_ones(self):
        resources = metabarcoding()
        resources['occurrence'] = pd.concat([resources['occurrence'], pd.DataFrame([
            {'occurrence_pk': 'o-c', 'occurrenceID': 'S1_ESV_3', 'event_fk': 'e1'}])])
        resources['identification'] = pd.concat([resources['identification'], pd.DataFrame([
            {'identification_pk': 'i3', 'occurrence_fk': 'o-c', 'nucleotideAnalysis_fk': 'a2'}])])

        rows, report = dna_derived_data(resources, 'occurrence', CORE)

        self.assertEqual(rows['_coreid'].tolist(), ['S1_ESV_1', 'S1_ESV_2'])
        self.assertEqual((report['not_projected'], report['dropped_links']), ({}, {'not_in_core': 1}))
        self.assertIn('1 further link(s) of projected analyses were dropped', describe(report))

    def test_event_core_takes_only_analyses_made_directly_on_an_event(self):
        resources = metabarcoding()
        resources['nucleotide-analysis'] = pd.concat([resources['nucleotide-analysis'],
                                                      pd.DataFrame([analysis('a3', 's1', material='')])])

        rows, report = dna_derived_data(resources, 'event', {'EV1'})

        self.assertEqual(rows['_coreid'].tolist(), ['EV1'])
        self.assertEqual(report['not_projected'], {'material_on_event_core': 2})
        self.assertIn('an Occurrence core keeps them', describe(report))

    def test_core_identifiers_never_confuse_one_records_id_with_another_records_key(self):
        # A=(pk 1, ID 2), B=(pk 2, ID x), C=(pk 3, no ID): "2" is A's ID and B's key.
        resources = metabarcoding()
        del resources['material']
        resources['occurrence'] = pd.DataFrame([{'occurrence_pk': '1', 'occurrenceID': '2'},
                                                {'occurrence_pk': '2', 'occurrenceID': 'x'},
                                                {'occurrence_pk': '3', 'occurrenceID': ''}])
        resources['nucleotide-analysis'] = pd.concat([resources['nucleotide-analysis'],
                                                      pd.DataFrame([analysis('a3', 's1')])])
        resources['identification'] = pd.DataFrame([{'occurrence_fk': '1', 'nucleotideAnalysis_fk': 'a1'},
                                                    {'occurrence_fk': '2', 'nucleotideAnalysis_fk': 'a2'},
                                                    {'occurrence_fk': '3', 'nucleotideAnalysis_fk': 'a3'}])

        rows, _ = dna_derived_data(resources, 'occurrence', {'1', '2', '3'}, id_mode='pk')
        self.assertEqual(rows['_coreid'].tolist(), ['1', '2', '3'])
        self.assertEqual(rows['DNA_sequence'].tolist(), ['ACGTAC', 'TTGACC', 'ACGTAC'])
        for core_ids in ({'2', '3'}, {'1', '2', '3'}, {'2', 'x', '3'}):
            rows, report = dna_derived_data(resources, 'occurrence', core_ids)
            self.assertEqual(rows['_coreid'].tolist(), ['3'], core_ids)
            self.assertEqual(report['not_projected'], {'ambiguous_core_id': 2}, core_ids)

    def test_core_of_dwc_dp_keys_resolves_without_an_explicit_mode(self):
        rows, _ = dna_derived_data(metabarcoding(), 'occurrence', {'o-host', 'o-a', 'o-b'})

        self.assertEqual(rows['_coreid'].tolist(), ['o-a', 'o-b'])

    def test_a_core_identifier_claimed_by_two_records_attaches_nothing(self):
        resources = metabarcoding()
        resources['occurrence'] = pd.DataFrame([{'occurrence_pk': 'o-a', 'occurrenceID': 'p'},
                                                {'occurrence_pk': 'p', 'occurrenceID': ''}])
        resources['identification'] = pd.DataFrame([{'occurrence_fk': 'o-a', 'nucleotideAnalysis_fk': 'a1'},
                                                    {'occurrence_fk': 'p', 'nucleotideAnalysis_fk': 'a2'}])

        rows, report = dna_derived_data(resources, 'occurrence', {'p'})

        self.assertIsNone(rows)
        self.assertEqual(report['not_projected'], {'ambiguous_core_id': 2})

    def test_material_with_several_evidence_candidates_is_not_attached(self):
        resources = metabarcoding()
        del resources['identification']
        resources['material'] = pd.DataFrame([{'materialEntity_pk': 'm1', 'evidenceForOccurrence_fk': 'o-host'},
                                              {'materialEntity_pk': 'm2', 'evidenceForOccurrence_fk': 'o-host'}])

        rows, report = dna_derived_data(resources, 'occurrence', CORE)

        self.assertIsNone(rows)
        self.assertEqual(report['not_projected'], {'ambiguous_material': 2})

    def test_unresolved_sequence_is_reported_and_protocol_facts_still_projected(self):
        resources = metabarcoding()
        resources['nucleotide-analysis'].loc[0, 'nucleotideSequence_fk'] = 'missing'

        rows, report = dna_derived_data(resources, 'occurrence', CORE)

        self.assertEqual(rows['DNA_sequence'].fillna('').tolist(), ['', 'TTGACC'])
        self.assertEqual(rows['target_gene'].tolist(), ['12S rRNA'] * 2)
        self.assertEqual(report['missing_references'], {'sequence': 1})

    def test_analysis_without_a_sequence_projects_its_protocol_facts(self):
        # A qPCR result has no sequence; DwC-DP 1.0 leaves nucleotideSequence_fk empty, which is not a broken link.
        resources = metabarcoding()
        resources['nucleotide-analysis'].loc[0, 'nucleotideSequence_fk'] = ''

        rows, report = dna_derived_data(resources, 'occurrence', CORE)

        self.assertEqual(rows['DNA_sequence'].fillna('').tolist(), ['', 'TTGACC'])
        self.assertEqual(rows['target_gene'].tolist(), ['12S rRNA'] * 2)
        self.assertEqual((report['missing_references'], report['without_sequence']), ({}, 1))
        self.assertIn('1 analyses have no sequence', describe(report))
        self.assertNotIn('Fix a missing DwC-DP link', describe(report))

    def test_no_analyses_give_no_rows(self):
        resources = metabarcoding()
        del resources['nucleotide-analysis']

        self.assertEqual(dna_derived_data(resources, 'occurrence', CORE)[0], None)


ENV = {'MINIO_URI': 'storage.example.org', 'MINIO_ACCESS_KEY': 'key', 'MINIO_SECRET_KEY': 'secret',
       'MINIO_BUCKET': 'bucket', 'MINIO_BUCKET_FOLDER': 'packages'}


def capture(target):
    def capture_archive(_client, _bucket, _name, local_path):
        with zipfile.ZipFile(local_path) as archive:
            target['meta'] = ET.fromstring(archive.read('meta.xml'))
            target['names'] = archive.namelist()
            if DNA_FILE in target['names']:
                target['dna'] = pd.read_csv(io.BytesIO(archive.read(DNA_FILE)), sep='\t', dtype=str,
                                            keep_default_na=False)
    return capture_archive


def row_types(meta):
    return {element.attrib['rowType'] for element in meta if element.tag.rsplit('}', 1)[-1] == 'extension'}


@patch('api.helpers.publish.Minio')
@patch('api.helpers.publish.upload_file')
class UploadDwcaDnaTests(TestCase):
    def setUp(self):
        task = Task.objects.create(name='Publication package preparation', text='Package', order=1)
        self.dataset = Dataset.objects.create(title='Faecal metabarcoding', description='eDNA test')
        self.agent = Agent.objects.create(dataset=self.dataset, task=task)
        self.core = Table.objects.create(dataset=self.dataset, title='dwca_occurrence_core', df=pd.DataFrame([
            {'occurrenceID': occurrence_id, 'basisOfRecord': 'MaterialSample', 'scientificName': name,
             'occurrenceStatus': 'present'}
            for occurrence_id, name in (('HOST', 'Panthera pardus'), ('S1_ESV_1', 'Tragelaphus scriptus'),
                                        ('S1_ESV_2', 'Acacia'))]))

    def add_dwc_dp(self, resources):
        for name, frame in resources.items():
            Table.objects.create(dataset=self.dataset, title=name, df=frame)

    def upload(self, extension_tables=None):
        with patch.dict(os.environ, ENV):
            return UploadDwCA(agent_id=self.agent.id, core_table_id=self.core.id,
                              core_type=DarwinCoreCoreType.OCCURRENCE, extension_tables=extension_tables).run()

    def test_dna_extension_is_derived_from_the_dwc_dp(self, upload_mock, _minio):
        self.add_dwc_dp(metabarcoding())
        captured = {}
        upload_mock.side_effect = capture(captured)

        result = self.upload()

        self.assertTrue(result.startswith('DwCA successfully created and uploaded: https://'), result)
        self.assertIn('DNA Derived Data: 2 row(s) for 2 core record(s) from 2 DwC-DP nucleotide analyses', result)
        self.assertIn(DNA_ROW_TYPE, row_types(captured['meta']))
        dna = captured['dna']
        self.assertEqual(dna.iloc[:, 0].tolist(), ['S1_ESV_1', 'S1_ESV_2'])
        self.assertEqual(dna['DNA_sequence'].tolist(), ['ACGTAC', 'TTGACC'])
        self.assertEqual(dna['target_gene'].tolist(), ['12S rRNA'] * 2)
        self.assertEqual(dna['pcr_primer_forward'].tolist(), ['GTCGGTAAAACTCGTGCCAGC'] * 2)

    def test_agent_built_dna_table_is_rejected_when_analyses_exist(self, upload_mock, _minio):
        self.add_dwc_dp(metabarcoding())
        manual = Table.objects.create(dataset=self.dataset, title='dwca_dna', df=pd.DataFrame([
            {'_coreid': 'S1_ESV_1', 'DNA_sequence': 'ACGTAC'}]))

        result = self.upload([{'table_id': manual.id, 'extension_type': DarwinCoreExtensionType.DNA_DERIVED_DATA,
                               'core_id_column': '_coreid'}])

        self.assertTrue(result.startswith('Error: ChatIPT builds the DNA Derived Data extension itself'), result)
        self.assertIn(str(manual.id), result)
        upload_mock.assert_not_called()

    def test_agent_built_dna_table_is_kept_without_dwc_dp_analyses(self, upload_mock, _minio):
        manual = Table.objects.create(dataset=self.dataset, title='dwca_dna', df=pd.DataFrame([
            {'_coreid': 'S1_ESV_1', 'DNA_sequence': 'ACGTAC', 'target_gene': 'COI'}]))
        captured = {}
        upload_mock.side_effect = capture(captured)

        result = self.upload([{'table_id': manual.id, 'extension_type': DarwinCoreExtensionType.DNA_DERIVED_DATA,
                               'core_id_column': '_coreid'}])

        self.assertTrue(result.startswith('DwCA successfully created and uploaded: https://'), result)
        self.assertNotIn('DNA Derived Data:', result)
        self.assertEqual(captured['dna']['target_gene'].tolist(), ['COI'])


class ProvisionalDnaTests(TestCase):
    @patch('api.quality_report._delete_archive')
    @patch('api.quality_report.agent_tools.submit_gbif_validation', return_value='k1')
    @patch('api.helpers.publish.upload_file')
    @patch('api.helpers.publish.Minio')
    def test_provisional_archive_carries_dna_rows_keyed_by_occurrence_pk(self, _minio, upload_mock, _submit, _delete):
        from api.quality_report import start_refinement_report

        task = Task.objects.create(name='Data validation and refinement', text='Validate', order=1)
        dataset = Dataset.objects.create(title='Refine', description='eDNA test')
        for name, frame in metabarcoding().items():
            Table.objects.create(dataset=dataset, title=name, df=frame)
        captured = {}
        upload_mock.side_effect = capture(captured)

        with patch.dict(os.environ, ENV):
            start_refinement_report(Agent.objects.create(dataset=dataset, task=task))

        self.assertIn(DNA_ROW_TYPE, row_types(captured['meta']))
        self.assertEqual(captured['dna'].iloc[:, 0].tolist(), ['o-a', 'o-b'])
        self.assertEqual(captured['dna']['DNA_sequence'].tolist(), ['ACGTAC', 'TTGACC'])
