import json
import zipfile
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from api import agent_tools
from api.agent_tools import ValidateDwCA, gbif_not_applicable, refresh_gbif_validation
from api.models import Agent, Dataset, Message, Task
from api.quality_report import prepare_gate_report, render_gbif_validation

DWC = 'http://rs.tdwg.org/dwc/terms/'
URL = 'https://example.org/archive.zip'


def meta_xml(core, extensions=()):
    tables = [f'<core rowType="{core}"><files><location>core.txt</location></files></core>'] if core else []
    tables += [f'<extension rowType="{row_type}"><files><location>x.txt</location></files></extension>'
               for row_type in extensions]
    return f'<archive xmlns="http://rs.tdwg.org/dwc/text/" metadata="eml.xml">{"".join(tables)}</archive>'


def archive_writer(meta):
    def download(url, local_path):
        with zipfile.ZipFile(local_path, 'w') as archive:
            archive.writestr('meta.xml', meta)
    return download


EVENT_ONLY = meta_xml(DWC + 'Event', [DWC + 'MeasurementOrFact'])


class RowTypeTests(TestCase):
    def setUp(self):
        self.dataset = Dataset.objects.create(title='Sites', dwca_url=URL)

    def test_row_types_are_read_once_per_archive(self):
        with patch('api.agent_tools._download_chatipt_archive', side_effect=archive_writer(EVENT_ONLY)) as download:
            first = agent_tools.dwca_row_types(self.dataset, URL)
            again = agent_tools.dwca_row_types(self.dataset, URL)

        download.assert_called_once()
        self.assertEqual(again, first)
        self.assertEqual(first, {'url': URL, 'core': DWC + 'Event', 'extensions': [DWC + 'MeasurementOrFact']})

    def test_only_event_core_without_occurrences_is_not_applicable(self):
        cases = {
            meta_xml(DWC + 'Event', [DWC + 'MeasurementOrFact']): True,
            meta_xml(DWC + 'Event', [DWC + 'Occurrence', DWC + 'MeasurementOrFact']): False,
            meta_xml(DWC + 'Occurrence'): False,
            meta_xml(DWC + 'Taxon'): False,
            meta_xml('http://rs.tdwg.org/eco/terms/Event'): False,
            meta_xml(None, [DWC + 'MeasurementOrFact']): False,
        }
        for index, (meta, expected) in enumerate(cases.items()):
            url = f'https://example.org/archive-{index}.zip'
            with self.subTest(meta=meta), \
                    patch('api.agent_tools._download_chatipt_archive', side_effect=archive_writer(meta)):
                self.assertEqual(bool(gbif_not_applicable(self.dataset, url)), expected)

    def test_unreadable_archive_is_submitted_as_before(self):
        with patch('api.agent_tools._download_chatipt_archive', side_effect=OSError('storage down')):
            self.assertIsNone(gbif_not_applicable(self.dataset, URL))


@patch('api.agent_tools._download_chatipt_archive', side_effect=archive_writer(EVENT_ONLY))
class NotApplicableWorkflowTests(TestCase):
    def setUp(self):
        self.task = Task.objects.create(name=Task.PREPUBLICATION_QUALITY_TASK, text='Check', order=1)
        self.dataset = Dataset.objects.create(title='Sites', description='Mires', dwca_url=URL)
        self.agent = Agent.create_with_system_message(dataset=self.dataset, task=self.task, tables=[])

    @patch('api.agent_tools.requests.post')
    def test_validate_dwca_does_not_submit_an_archive_without_occurrences(self, post, _download):
        result = json.loads(ValidateDwCA(agent_id=self.agent.id).run())

        post.assert_not_called()
        self.assertEqual(result['status'], 'NOT_APPLICABLE')
        self.dataset.refresh_from_db()
        self.assertTrue(self.dataset.has_current_dwca_validation)

    @patch('api.agent_tools.requests.get')
    def test_resuming_a_stuck_key_reports_not_applicable(self, get, _download):
        self.dataset.dwca_validation = {'url': URL, 'key': 'd6a4', 'status': 'QUEUED', 'aborted_keys': ['b11d']}
        self.dataset.save(update_fields=['dwca_validation'])

        result = json.loads(ValidateDwCA(agent_id=self.agent.id, validation_key='d6a4').run())

        get.assert_not_called()
        self.assertEqual(result['status'], 'NOT_APPLICABLE')

    @patch('api.agent_tools.requests.get')
    def test_waiting_validation_becomes_not_applicable_without_polling_gbif(self, get, _download):
        self.dataset.dwca_validation = {'url': URL, 'key': 'd6a4', 'status': 'QUEUED'}
        self.dataset.save(update_fields=['dwca_validation'])

        result = json.loads(refresh_gbif_validation(self.dataset, {
            'validation_key': 'd6a4', 'url': URL, 'waiting_since': timezone.now().isoformat(),
        }))

        get.assert_not_called()
        self.assertEqual(result['status'], 'NOT_APPLICABLE')

    @patch('api.agent_tools.requests.post')
    def test_twice_aborted_archive_is_reported_not_applicable(self, post, _download):
        record = agent_tools.resubmit_aborted_gbif_validation(self.dataset, {
            'url': URL, 'key': 'd6a4', 'status': 'ABORTED', 'aborted_keys': ['b11d'],
        })

        post.assert_not_called()
        self.assertEqual(record['status'], 'NOT_APPLICABLE')

    @patch('api.quality_report.agent_tools.inspect_publication_artifacts', return_value={'preflight': {'valid': True}})
    @patch('api.quality_report.agent_tools.submit_gbif_validation')
    def test_gate_report_explains_instead_of_submitting(self, submit, _inspect, _download):
        self.assertTrue(prepare_gate_report(self.agent))

        submit.assert_not_called()
        report = Message.objects.get(agent=self.agent, openai_obj__quality_report=True).openai_obj['content']
        self.assertIn('GBIF validation does not apply to this archive', report)
        self.assertIn('no occurrence records from it will appear on GBIF', report)

    @patch('api.quality_report.agent_tools.inspect_publication_artifacts', return_value={'preflight': {'valid': True}})
    @patch('api.agent_tools.requests.get')
    def test_gate_stops_waiting_on_an_existing_key(self, get, _inspect, _download):
        self.dataset.dwca_validation = {
            'url': URL, 'key': 'd6a4', 'status': 'QUEUED', 'submitted_at': timezone.now().isoformat(),
        }
        self.dataset.save(update_fields=['dwca_validation'])

        self.assertTrue(prepare_gate_report(self.agent))

        get.assert_not_called()
        self.dataset.refresh_from_db()
        self.assertEqual(self.dataset.dwca_validation['status'], 'NOT_APPLICABLE')

    def test_not_applicable_counts_only_for_the_current_archive(self, _download):
        record = gbif_not_applicable(self.dataset, URL)
        self.dataset.refresh_from_db()
        self.assertTrue(self.dataset.has_current_dwca_validation)
        self.assertIn('GBIF validation does not apply', render_gbif_validation(record))

        self.dataset.dwca_url = 'https://example.org/rebuilt.zip'
        self.assertFalse(self.dataset.has_current_dwca_validation)
