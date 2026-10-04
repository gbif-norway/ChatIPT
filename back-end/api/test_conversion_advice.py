import json
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APIClient

from api.conversion_jobs import pending_advice, process_next_conversion
from api.models import CustomUser, DwcConversion, DwcConversionJob


@override_settings(OPENAI_API_KEY='test', OPENAI_MODEL_EFFICIENT='gpt-6-luna')
class AdviceBatchTests(TransactionTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        storage = override_settings(STORAGES={
            'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage', 'OPTIONS': {'location': folder.name}},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
        })
        storage.enable(); self.addCleanup(storage.disable)
        self.client = APIClient()
        self.client.force_authenticate(CustomUser.objects.create_user(username='advice-owner'))
        response = self.client.post('/api/datasets/', {'workflow_type': 'dwca_conversion', 'files': [
            # Distinct scope claims stay separate questions; identical row questions would be grouped.
            SimpleUploadedFile('event.csv', ('eventID,eventCategory\n' + ''.join(f'e{n},survey\n' for n in range(50))).encode()),
            SimpleUploadedFile('humboldt.csv', ('eventID,targetTaxonomicScope\n' + ''.join(f'e{n},Taxon{n}\n' for n in range(50))).encode()),
        ]}, format='multipart')
        self.assertEqual(response.status_code, 201, response.data)
        process_next_conversion()
        self.conversion = DwcConversion.objects.get(dataset_id=response.data['id'])
        self.url = f'/api/datasets/{response.data["id"]}/conversion/'

    def request(self, action='suggest', decisions=None):
        return self.client.post(self.url, {
            'action': action, 'plan_id': self.conversion.plan['id'], 'decisions': decisions or {},
        }, format='json')

    def test_successive_batches_keep_advice_skip_answered_and_record_abstentions(self):
        first = self.conversion.plan['issues'][0]
        decisions = {first['id']: first['options'][0]['value']}
        expected = [issue['id'] for issue in pending_advice(self.conversion) if issue['id'] != first['id']]
        self.assertGreater(len(expected), 40)
        batches = []

        def response(payload):
            issues = json.loads(payload['input'][1]['content'])['issues']
            batches.append([issue['id'] for issue in issues])
            items = [{'id': issue['id'], 'option': issue['options'][0]['value'], 'reason': 'Review source meaning'}
                     for issue in issues]
            if len(batches) == 1:
                items.pop()  # The model may abstain; that issue must not block later batches.
                items.append({'id': expected[-1], 'option': 'preserve', 'reason': 'Outside this batch'})
            return SimpleNamespace(id=f'batch-{len(batches)}', status='completed', model='gpt-6-luna',
                                   usage={}, output_text=json.dumps({'suggestions': items}))

        with patch('api.helpers.openai_helpers.query_responses_api', side_effect=response) as query:
            self.assertEqual(self.request(decisions=decisions).status_code, 202)
            process_next_conversion(); self.conversion.refresh_from_db()
            earlier = self.conversion.suggestions.copy()
            self.assertEqual(len(earlier), 39)
            self.assertEqual(self.conversion.decisions, decisions)
            state = self.client.get(self.url).data
            self.assertEqual(state['advice_progress']['reviewed'], 40)
            self.assertEqual(state['advice_progress']['without_suggestion'], 1)
            self.assertEqual(self.request(decisions=decisions).status_code, 202)
            process_next_conversion(); self.conversion.refresh_from_db()
            self.assertEqual(self.conversion.suggestions[:39], earlier)
            self.assertEqual([*batches[0], *batches[1]], expected)
            self.assertEqual(self.conversion.advice_reviewed, expected)
            self.assertEqual(len(pending_advice(self.conversion)), 0)
            self.assertEqual(self.request(decisions=decisions).status_code, 200)
            self.assertFalse(DwcConversionJob.objects.filter(conversion=self.conversion).exists())
            self.assertEqual(query.call_count, 2)
            self.assertEqual(self.request('convert', decisions).status_code, 400)  # Advice is not approval.

        self.assertEqual(self.request('inspect').status_code, 202)
        process_next_conversion(); self.conversion.refresh_from_db()
        self.assertEqual(self.conversion.advice_reviewed, [])
        self.assertEqual(self.conversion.suggestions, [])

    def test_preserved_extensions_are_not_sent_and_drafts_are_saved_when_exhausted(self):
        decisions = {issue['id']: issue['options'][0]['value'] for issue in self.conversion.plan['issues']
                     if 'table' not in issue or issue['id'] == 'table:1'}
        decisions['table:1'] = 'preserve'
        with patch('api.helpers.openai_helpers.query_responses_api') as query:
            state = self.request(decisions=decisions)
            self.assertEqual(state.status_code, 200)
            self.assertEqual(state.data['advice_progress']['remaining'], 0)
            query.assert_not_called()
        self.conversion.refresh_from_db()
        self.assertEqual(self.conversion.decisions, decisions)

    def test_failed_batch_can_be_retried_without_losing_existing_suggestions(self):
        self.conversion.suggestions = [{'id': 'loose-links', 'option': 'confirm', 'reason': 'Existing advice'}]
        self.conversion.save()
        expected = [issue['id'] for issue in pending_advice(self.conversion)[:40]]
        failure = SimpleNamespace(id='failed-batch', status='incomplete', model='gpt-6-luna', usage={})
        with patch('api.helpers.openai_helpers.query_responses_api', return_value=failure):
            self.assertEqual(self.request().status_code, 202)
            process_next_conversion()
        self.conversion.refresh_from_db()
        self.assertEqual(self.conversion.advice_reviewed, [])
        self.assertEqual(len(self.conversion.suggestions), 1)
        self.assertEqual([issue['id'] for issue in pending_advice(self.conversion)[:40]], expected)
        self.assertEqual(self.conversion.status, 'review')
