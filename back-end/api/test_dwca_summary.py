from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from api.dwca_summary import dwca_summary, row_type_label, validation_state
from api.models import CustomUser, Dataset

URL = 'https://example.org/dwca-2.zip'
OLD_URL = 'https://example.org/dwca-1.zip'
OCCURRENCE = 'http://rs.tdwg.org/dwc/terms/Occurrence'
EVENT = 'http://rs.tdwg.org/dwc/terms/Event'
MOF = 'http://rs.tdwg.org/dwc/terms/MeasurementOrFact'
DNA = 'http://rs.gbif.org/terms/1.0/DNADerivedData'


def finished(url=URL, indexable=True, files=()):
    return {'url': url, 'status': 'FINISHED', 'metrics': {'indexeable': indexable, 'files': list(files)}}


class RowTypeLabelTests(SimpleTestCase):
    def test_known_row_types_use_familiar_names(self):
        self.assertEqual(row_type_label(MOF), 'Measurement or fact')
        self.assertEqual(row_type_label(DNA), 'DNA derived data')

    def test_unknown_row_types_are_split_into_words(self):
        self.assertEqual(row_type_label('http://rs.tdwg.org/dwc/terms/ResourceRelationship'), 'Resource relationship')
        self.assertEqual(row_type_label('http://example.org/terms/GGBNMaterialSample'), 'GGBN material sample')
        self.assertEqual(row_type_label(None), 'Unknown table')


class ValidationStateTests(SimpleTestCase):
    def test_no_result_or_a_result_for_an_older_archive_is_not_checked(self):
        self.assertEqual(validation_state(None, URL), 'not_checked')
        self.assertEqual(validation_state(finished(url=OLD_URL), URL), 'not_checked')

    def test_running_and_queued_are_checking(self):
        self.assertEqual(validation_state({'url': URL, 'status': 'QUEUED'}, URL), 'checking')
        self.assertEqual(validation_state({'url': URL, 'status': 'RUNNING'}, URL), 'checking')

    def test_finished_passes_only_when_gbif_can_index_it(self):
        self.assertEqual(validation_state(finished(), URL), 'passed')
        self.assertEqual(validation_state(finished(indexable=False), URL), 'failed')
        self.assertEqual(validation_state({'url': URL, 'status': 'ABORTED'}, URL), 'failed')

    def test_archives_without_occurrences_are_not_applicable(self):
        self.assertEqual(validation_state({'url': URL, 'status': 'NOT_APPLICABLE'}, URL), 'not_applicable')


class DwcaSummaryTests(SimpleTestCase):
    def test_no_archive_has_no_summary(self):
        self.assertIsNone(dwca_summary(''))

    def test_tables_come_from_meta_xml_with_counts_from_gbif(self):
        summary = dwca_summary(
            URL,
            finished(files=[
                {'fileName': 'event.txt', 'rowType': EVENT, 'count': 12, 'fileType': 'CORE'},
                {'fileName': 'occurrence.txt', 'rowType': OCCURRENCE, 'count': 340, 'fileType': 'EXTENSION'},
                {'fileName': 'eml.xml', 'fileType': 'METADATA'},
            ]),
            {'url': URL, 'core': EVENT, 'extensions': [OCCURRENCE, MOF]},
        )
        self.assertEqual(summary['tables'], [
            {'kind': 'core', 'row_type': EVENT, 'label': 'Event', 'rows': 12},
            {'kind': 'extension', 'row_type': OCCURRENCE, 'label': 'Occurrence', 'rows': 340},
            {'kind': 'extension', 'row_type': MOF, 'label': 'Measurement or fact', 'rows': None},
        ])
        self.assertEqual(summary['validation'], {'state': 'passed', 'message': ''})

    def test_counts_from_an_older_archive_are_not_shown(self):
        summary = dwca_summary(
            URL,
            finished(url=OLD_URL, files=[{'rowType': OCCURRENCE, 'count': 99, 'fileType': 'CORE'}]),
            {'url': URL, 'core': OCCURRENCE, 'extensions': []},
        )
        self.assertEqual(summary['tables'], [
            {'kind': 'core', 'row_type': OCCURRENCE, 'label': 'Occurrence', 'rows': None},
        ])
        self.assertEqual(summary['validation']['state'], 'not_checked')

    def test_without_meta_xml_row_types_the_gbif_files_name_the_tables(self):
        summary = dwca_summary(URL, finished(files=[
            {'rowType': MOF, 'count': 5, 'fileType': 'EXTENSION'},
            {'rowType': OCCURRENCE, 'count': 40, 'fileType': 'CORE'},
        ]), {'url': OLD_URL, 'core': EVENT, 'extensions': []})
        self.assertEqual([(table['kind'], table['label'], table['rows']) for table in summary['tables']], [
            ('core', 'Occurrence', 40),
            ('extension', 'Measurement or fact', 5),
        ])

    def test_not_applicable_keeps_the_explanation(self):
        summary = dwca_summary(
            URL,
            {'url': URL, 'status': 'NOT_APPLICABLE', 'message': 'No occurrence records for GBIF to check.'},
            {'url': URL, 'core': EVENT, 'extensions': []},
        )
        self.assertEqual(summary['validation'], {
            'state': 'not_applicable',
            'message': 'No occurrence records for GBIF to check.',
        })


class DatasetApiDwcaSummaryTests(TestCase):
    def test_dataset_detail_includes_the_archive_summary(self):
        user = CustomUser.objects.create_user(username='owner', email='owner@example.org', password='x')
        dataset = Dataset.objects.create(
            user=user,
            title='Plots',
            dwca_url=URL,
            dwca_validation=finished(files=[{'rowType': OCCURRENCE, 'count': 7, 'fileType': 'CORE'}]),
            dwca_row_types={'url': URL, 'core': OCCURRENCE, 'extensions': []},
        )
        client = APIClient()
        client.force_authenticate(user)
        response = client.get(f'/api/datasets/{dataset.id}/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['dwca_summary']['tables'], [
            {'kind': 'core', 'row_type': OCCURRENCE, 'label': 'Occurrence', 'rows': 7},
        ])
        self.assertEqual(response.json()['dwca_summary']['validation']['state'], 'passed')
