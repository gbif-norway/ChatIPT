"""The conversion value interpreter is exercised with mocked model responses only."""
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from api import conversion_tidy
from api.conversion_jobs import process_next_conversion
from api.dwca_import import DWC, SourceArchive, SourceTable
from api.dwca_tidy import tidy_archive
from api.models import CustomUser, Dataset, DwcConversion, DwcConversionJob, OpenAIUsage
from api.test_conversion_review import AI, ConversionTestCase


def archive(rows, extra_terms=()):
    terms = [DWC + name for name in ('occurrenceID', 'sex', 'eventRemarks', 'individualCount', 'stateProvince',
                                      'scientificName', 'occurrenceStatus', *extra_terms)]
    return SourceArchive({}, [SourceTable('occurrence', DWC + 'Occurrence', terms, rows, [], True)],
                         'fingerprint', False, {})


class TidyModelPureTests(SimpleTestCase):
    def test_candidates_bound_unresolved_values_and_filter_places(self):
        source = archive([
            ['o1', 'f', 'ad', '1', '', 'Aus bus', 'present'],
            ['o2', 'f', 'fad', '2', 'M�re og Romsdal', 'Aus bus', 'present'],
            ['o3', '', 'North Sea', '1', 'Norway', 'Aus bus', 'present'],
            ['o4', '', '1 juv.', '2', '', 'Aus bus', 'present'],
        ])
        view = tidy_archive(source)[0]
        found = conversion_tidy.candidates(view)
        by_field = {item['field']: item for item in found}
        self.assertNotIn('ad', [v['value'] for v in by_field['eventRemarks']['values']])
        remark_values = [v['value'] for v in by_field['eventRemarks']['values']]
        self.assertIn('fad', remark_values)
        self.assertIn('1 juv.', remark_values)
        self.assertNotIn('ad', remark_values)
        self.assertEqual([v['value'] for v in by_field['stateProvince']['values']], ['M�re og Romsdal', 'Norway'])
        self.assertIn('sex', [item['term'] for item in by_field['eventRemarks']['context']['siblings']])
        self.assertNotIn('scientificName', by_field)

        many = archive([[f'o{i}', '', f'free {i}', '1', '', 'Aus bus', 'present'] for i in range(400)])
        many_view = tidy_archive(many)[0]
        self.assertNotIn('eventRemarks', {item['field'] for item in conversion_tidy.candidates(many_view)})

    def test_parse_response_drops_invalid_values_and_keeps_abstentions(self):
        source = archive([['o1', '', 'fad', '2', '', 'Aus bus', 'present']])
        columns = conversion_tidy.candidates(tidy_archive(source)[0])
        remarks = next(item for item in columns if item['field'] == 'eventRemarks')
        key = remarks['key']
        payload = {'columns': [{'key': key, 'verdict': 'remarks describe organism', 'values': [
            {'i': 0, 'fields': [{'field': 'sex', 'value': 'woman'}, {'field': 'individualCount', 'value': '1.5'},
                                {'field': 'countryCode', 'value': 'Norway'}, {'field': 'scientificName', 'value': 'Aus bus'}],
             'residue': '', 'confidence': 'high', 'note': ''},
            {'i': 0, 'fields': [], 'residue': '', 'confidence': 'low', 'note': 'unclear'},
            {'i': 99, 'fields': [], 'residue': '', 'confidence': 'low', 'note': ''},
        ]}, {'key': 'unknown', 'verdict': 'ignore', 'values': []}]}
        response = SimpleNamespace(status='completed', output_text=json.dumps(payload))
        entries, verdicts = conversion_tidy.parse_response(response, columns)
        self.assertEqual(list(entries), [remarks['values'][0]['key']])
        self.assertEqual(entries[remarks['values'][0]['key']]['fields'], {})
        self.assertEqual(entries[remarks['values'][0]['key']]['confidence'], 'high')
        self.assertEqual(verdicts[key], 'remarks describe organism')

    def test_model_change_tiers_and_verbatim_residue(self):
        source = archive([
            ['o1', 'f', 'fad', '1', '', 'Aus bus', 'present'],
            ['o2', 'f', '1 juv.', '2', '', 'Aus bus', 'present'],
            ['o3', '', 'ad + egg', '1', '', 'Aus bus', 'present'],
            ['o4', '', '', '1', 'M�re og Romsdal', 'Aus bus', 'present'],
        ])
        entries = {
            'fad': {'table': 0, 'column': 2, 'value': 'fad', 'fields': {'lifeStage': 'adult', 'sex': 'female'},
                    'residue': '', 'confidence': 'medium', 'note': 'abbreviation'},
            'juv': {'table': 0, 'column': 2, 'value': '1 juv.', 'fields': {'lifeStage': 'juvenile', 'individualCount': '1'},
                    'residue': '', 'confidence': 'high', 'note': ''},
            'egg': {'table': 0, 'column': 2, 'value': 'ad + egg', 'fields': {'lifeStage': 'adult'},
                    'residue': 'egg', 'confidence': 'high', 'note': ''},
            'place': {'table': 0, 'column': 4, 'value': 'M�re og Romsdal', 'fields': {'stateProvince': 'Møre og Romsdal'},
                      'residue': '', 'confidence': 'high', 'note': ''},
            'low': {'table': 0, 'column': 2, 'value': 'never', 'fields': {'lifeStage': 'adult'},
                    'residue': '', 'confidence': 'low', 'note': ''},
        }
        changes = conversion_tidy.model_changes(source, entries)
        tiers = {item['value']: item['tier'] for item in changes}
        self.assertEqual(tiers['fad'], 'auto')
        self.assertEqual(tiers['1 juv.'], 'suggest')
        self.assertNotIn('never', tiers)
        egg = next(item for item in changes if item['value'] == 'ad + egg')
        self.assertTrue(egg['move'])
        self.assertEqual(egg['fields']['eventRemarks'], '')
        self.assertEqual(egg['fields']['occurrenceRemarks'], 'ad + egg')
        self.assertIn('Møre og Romsdal', str(next(item for item in changes if item['value'] == 'M�re og Romsdal')))
        # A remark about the organism keeps its exact words in occurrenceRemarks.
        fad = next(item for item in changes if item['value'] == 'fad')
        self.assertEqual((fad['fields']['eventRemarks'], fad['fields']['occurrenceRemarks'], fad['move']), ('', 'fad', True))
        view = tidy_archive(source, model_changes=changes)[0]
        row = dict(zip([term.rsplit('/', 1)[-1] for term in view.tables[0].terms], view.tables[0].rows[0]))
        self.assertEqual((row['eventRemarks'], row['occurrenceRemarks'], row['lifeStage'], row['sex']), ('', 'fad', 'adult', 'female'))
        self.assertEqual(view.tables[0].rows[3][4], 'Møre og Romsdal')

    def test_uncertain_value_keeps_its_words_and_added_columns_are_never_sent(self):
        # 568: 'Female?' reads as female; the question mark is left over, so the exact text goes to occurrenceRemarks.
        source = archive([['o1', 'Female?', '', '1', '', 'Aus bus', 'present']])
        entries = {'q': {'table': 0, 'column': 1, 'value': 'Female?', 'fields': {'sex': 'female'}, 'residue': '?',
                         'confidence': 'medium', 'note': 'uncertain'}}
        change = conversion_tidy.model_changes(source, entries)[0]
        self.assertEqual((change['fields'], change['tier']), ({'sex': 'female', 'occurrenceRemarks': 'Female?'}, 'suggest'))
        # 570: the tidy-up appends waterBody for sea names in countryCode; that added column is not a model candidate.
        sea = SourceArchive({}, [SourceTable('occurrence', DWC + 'Occurrence', [DWC + 'occurrenceID', DWC + 'countryCode'],
                                             [['o1', 'North Atlantic Ocean (other parts)'], ['o2', 'Norway']], [], True)], 'fp', False, {})
        view = tidy_archive(sea)[0]
        self.assertEqual(view.tables[0].terms[-1], DWC + 'waterBody')
        self.assertEqual(conversion_tidy.candidates(view), [])
        self.assertEqual(conversion_tidy.model_changes(sea, {'x': {'table': 0, 'column': 9, 'value': 'v', 'fields': {'sex': 'male'},
                                                                   'residue': '', 'confidence': 'high', 'note': ''}}), [])


# The AI reviewer stays out of the way (no automatic review runs), so only the tidy-up calls the model.
class TidyModelSafetyTests(SimpleTestCase):
    def columns(self, source):
        return conversion_tidy.candidates(tidy_archive(source)[0])

    def answer(self, columns, field, text, fields, confidence='high', residue=''):
        column = next(item for item in columns if item['field'] == field)
        value = next(item for item in column['values'] if item['value'] == text)
        return {'key': column['key'], 'verdict': '', 'values': [{'i': value['i'], 'fields': [
            {'field': name, 'value': wanted} for name, wanted in fields.items()], 'residue': residue,
            'confidence': confidence, 'note': ''}]}

    def parse(self, columns, *answers):
        return conversion_tidy.parse_response(SimpleNamespace(status='completed', output_text=json.dumps({'columns': list(answers)})),
                                              columns)[0]

    def test_answers_are_limited_to_plausible_fields_and_vocabulary_concepts(self):
        source = archive([['o1', '', '', '1', '', 'Aus bus', 'present', 'foraging', 'nativeish']],
                         extra_terms=('behavior', 'establishmentMeans'))
        columns = self.columns(source)
        entries = self.parse(columns, self.answer(columns, 'behavior', 'foraging', {'countryCode': 'NO', 'behavior': 'feeding'}),
                             self.answer(columns, 'establishmentMeans', 'nativeish', {'establishmentMeans': 'nativeish'}))
        fields = {entry['value']: entry['fields'] for entry in entries.values()}
        self.assertEqual(fields, {'foraging': {'behavior': 'feeding'}, 'nativeish': {}})

    def test_risky_model_changes_are_only_suggested(self):
        source = archive([['o1', '', 'seen twice', '1', 'M\ufffdre og Romsdal', 'Aus bus', 'present', 'ind/m3', 'CV', 'flying']],
                         extra_terms=('organismQuantityType', 'lifeStage', 'behavior'))

        def entry(column, value, fields, confidence='high'):
            return {'table': 0, 'column': column, 'value': value, 'fields': fields, 'residue': '', 'confidence': confidence, 'note': ''}
        changes = conversion_tidy.model_changes(source, {
            'clear': entry(9, 'flying', {'behavior': ''}),
            'unit': entry(7, 'ind/m3', {'organismQuantityType': 'individuals per cubic metre'}),
            'place': entry(4, 'M\ufffdre og Romsdal', {'stateProvince': 'Møre og Romsdal'}),
            'stage': entry(8, 'CV', {'lifeStage': 'copepodite V'}),
            'remark': entry(2, 'seen twice', {'lifeStage': 'copepodite V'}),
        })
        tiers = {change['value']: change['tier'] for change in changes}
        self.assertEqual(tiers, {'flying': 'suggest', 'ind/m3': 'suggest', 'M\ufffdre og Romsdal': 'auto', 'CV': 'auto',
                                 'seen twice': 'suggest'})

    def test_a_place_restated_in_another_field_moves_there(self):
        source = archive([['o1', '', '', '1', 'Norway', 'Aus bus', 'present']])
        change = conversion_tidy.model_changes(source, {'n': {'table': 0, 'column': 4, 'value': 'Norway', 'residue': '',
            'fields': {'country': 'Norway', 'countryCode': 'NO'}, 'confidence': 'high', 'note': ''}})[0]
        self.assertEqual((change['fields'], change['move'], change['tier']),
                         ({'country': 'Norway', 'countryCode': 'NO', 'stateProvince': ''}, True, 'auto'))

    def test_review_round_five_cases(self):
        # A life stage in the sex column is sent, a custom (non-Darwin Core) sex column is not.
        source = archive([['o1', 'juvenile', '', '1', '', 'Aus bus', 'present', 'f']], extra_terms=())
        source.tables[0].terms.append('http://example.org/terms/sex')
        sent = {column['field']: [value['value'] for value in column['values']] for column in self.columns(source)}
        self.assertEqual(sent, {'sex': ['juvenile']})

        def entry(column, value, fields, confidence='high'):
            return {'table': 0, 'column': column, 'value': value, 'fields': fields, 'residue': '', 'confidence': confidence, 'note': ''}
        # occurrenceRemarks only ever receives the exact source text; other free text written elsewhere is a suggestion.
        behaviour = archive([['o1', '', '', '1', '', 'Aus bus', 'present', 'foraging']], extra_terms=('behavior',))
        changes = conversion_tidy.model_changes(behaviour, {
            'a': entry(7, 'foraging', {'occurrenceRemarks': 'nocturnal', 'reproductiveCondition': 'breeding'})})
        self.assertEqual((changes[0]['fields'], changes[0]['tier']),
                         ({'reproductiveCondition': 'breeding', 'behavior': 'foraging'}, 'suggest'))
        # The same remark copied into occurrenceRemarks is no evidence for a medium reading.
        twice = archive([['o1', '', 'fad', '1', '', 'Aus bus', 'present', 'fad']], extra_terms=('occurrenceRemarks',))
        changes = conversion_tidy.model_changes(twice, {'a': entry(2, 'fad', {'lifeStage': 'adult', 'sex': 'female'}, 'medium')})
        self.assertEqual(changes[0]['tier'], 'suggest')

    def test_answers_about_spaced_values_are_corroborated_and_applied(self):
        source = archive([['o1', 'f', 'fad ', '1', '', 'Aus bus', 'present']])
        view = tidy_archive(source)[0]
        remarks = next(column for column in conversion_tidy.candidates(view) if column['field'] == 'eventRemarks')
        self.assertEqual([value['value'] for value in remarks['values']], ['fad'])
        change = conversion_tidy.model_changes(source, {'k': {'table': 0, 'column': 2, 'value': 'fad', 'residue': '',
            'fields': {'lifeStage': 'adult', 'sex': 'female'}, 'confidence': 'medium', 'note': ''}})[0]
        self.assertEqual(change['tier'], 'auto')  # sex f in the row agrees
        self.assertEqual(tidy_archive(source, model_changes=[change])[0].tables[0].rows[0][2], '')

    def test_values_longer_than_the_model_sees_are_not_sent(self):
        source = archive([['o1', '', '', '1', 'M\ufffdre og Romsdal, ' + 'x' * 200, 'Aus bus', 'present']])
        self.assertEqual(conversion_tidy.candidates(tidy_archive(source)[0]), [])

    def test_review_round_nine_cases(self):
        # An event remark the model does not read as describing the organism stays where it is.
        source = archive([['o1', '', 'Sampling failed', '1', '', 'Aus bus', 'present']])
        self.assertEqual(conversion_tidy.model_changes(source, {'k': {'table': 0, 'column': 2, 'value': 'Sampling failed',
            'fields': {}, 'residue': 'Sampling failed', 'confidence': 'high', 'note': ''}}), [])
        # Neighbouring values are clipped, and a column too large for one call is left out.
        long = archive([['o1', 'Female?', '', '1', '', 'Aus bus', 'present', 'x' * 5000]], extra_terms=('occurrenceRemarks',))
        column = next(item for item in conversion_tidy.candidates(tidy_archive(long)[0]) if item['field'] == 'sex')
        remarks = next(item for item in column['context']['siblings'] if item['term'] == 'occurrenceRemarks')
        self.assertEqual(len(remarks['values'][0]['value']), conversion_tidy.SIBLING_CHARS)
        with patch.object(conversion_tidy, 'REQUEST_CHARS', 50):
            self.assertEqual(conversion_tidy.candidates(tidy_archive(long)[0]), [])

    def test_model_answers_never_corroborate_each_other(self):
        # Both remarks columns propose lifeStage adult for a row whose own lifeStage is empty: nothing in the source agrees.
        source = archive([['o1', '', 'fad', '1', '', 'Aus bus', 'present', 'adult female']], extra_terms=('occurrenceRemarks',))

        def entry(column, value):
            return {'table': 0, 'column': column, 'value': value, 'fields': {'lifeStage': 'adult'}, 'residue': '',
                    'confidence': 'medium', 'note': ''}
        changes = conversion_tidy.model_changes(source, {'a': entry(2, 'fad'), 'b': entry(7, 'adult female')})
        self.assertEqual({change['tier'] for change in changes}, {'suggest'})


@override_settings(**AI, CONVERSION_NAME_CHECKS_ENABLED=False, CONVERSION_REVIEW_MAX_RUNS_PER_PLAN=0)
class TidyModelFlowTests(ConversionTestCase):
    files = [('occurrence.csv', b'occurrenceID,sex,eventRemarks,individualCount,lifeStage,scientificName,occurrenceStatus\n'
              b'o1,f,ad,1,,Aus bus,present\n'
              b'o2,f,fad,1,,Aus bus,present\n'
              b'o3,,1 juv.,2,,Aus bus,present\n')]

    def test_inspect_chains_tidy_once_and_stores_model_usage(self):
        def response(args, max_retries=None):
            payload = json.loads(args['input'][1]['content'])
            self.assertIn('columns', payload)
            col = next(item for item in payload['columns'] if item['term'].endswith('/eventRemarks'))
            answers = []
            for value in col['values']:
                answer = {'i': value['i'], 'fields': [], 'residue': '', 'confidence': 'low', 'note': 'unclear'}
                if value['text'] == 'fad':
                    answer = {'i': value['i'], 'fields': [{'field': 'lifeStage', 'value': 'adult'},
                        {'field': 'sex', 'value': 'female'}], 'residue': '', 'confidence': 'medium',
                        'note': 'supported by sex'}
                answers.append(answer)
            output = {'columns': [{'key': col['key'], 'verdict': 'remarks describe organism', 'values': answers}]}
            return SimpleNamespace(id='tidy-response', status='completed', model='gpt-6-sol',
                usage={'input_tokens': 100, 'output_tokens': 50}, output_text=json.dumps(output))

        with patch('api.helpers.openai_helpers.query_with_flex_fallback', side_effect=response) as query:
            process_next_conversion()
            self.assertEqual(DwcConversionJob.objects.get(conversion=self.conversion).action, 'tidy')
            self.assertEqual(self.conversion.status, 'inspecting')
            process_next_conversion()
            self.assertEqual(self.conversion.status, 'review')
            self.assertTrue(any(group['by'] == 'model' for group in self.conversion.tidy['summary']['groups']))
            self.assertTrue(OpenAIUsage.objects.filter(response_id='tidy-response', task_name=conversion_tidy.TIDY_TASK).exists())
            self.post('inspect')
            process_next_conversion()
            process_next_conversion()
            self.assertEqual(query.call_count, 1)

    def test_model_answers_survive_convert_and_are_reused_only_for_the_same_owner(self):
        def response(args, max_retries=None):
            col = next(item for item in json.loads(args['input'][1]['content'])['columns'] if item['term'].endswith('/eventRemarks'))
            answers = [{'i': value['i'], 'fields': [{'field': 'lifeStage', 'value': 'adult'}, {'field': 'sex', 'value': 'female'}]
                        if value['text'] == 'fad' else [], 'residue': '', 'confidence': 'high' if value['text'] == 'fad' else 'low',
                        'note': ''} for value in col['values']]
            return SimpleNamespace(id='tidy-response', status='completed', model='gpt-6-sol',
                                   usage={'input_tokens': 100, 'output_tokens': 50},
                                   output_text=json.dumps({'columns': [{'key': col['key'], 'verdict': '', 'values': answers}]}))
        with patch('api.helpers.openai_helpers.query_with_flex_fallback', side_effect=response):
            process_next_conversion(); process_next_conversion()
        conversion = self.conversion
        decisions = {item['id']: item['options'][0]['value'] for item in conversion.plan['issues'] if item.get('options')}
        self.assertEqual(self.post('convert', decisions=decisions).status_code, 202)
        process_next_conversion()
        conversion = self.conversion
        self.assertEqual(conversion.status, 'complete', conversion.error)
        self.assertTrue(any(group['by'] == 'model' for group in conversion.report['tidy']['groups']))
        fingerprint = conversion.plan['source_sha256']
        other = DwcConversion.objects.create(dataset=Dataset.objects.create(
            user=CustomUser.objects.create_user(username='someone-else'), workflow_type='dwca_conversion'))
        self.assertEqual(conversion_tidy._model_cache(other, fingerprint), {})
        same = DwcConversion.objects.create(dataset=Dataset.objects.create(user=conversion.dataset.user, workflow_type='dwca_conversion'))
        self.assertTrue(conversion_tidy._model_cache(same, fingerprint)['entries'])

    def test_values_left_out_of_an_answer_are_not_asked_again(self):
        def response(args, max_retries=None):
            return SimpleNamespace(id='partial', status='completed', model='gpt-6-sol', usage={'input_tokens': 10, 'output_tokens': 5},
                                   output_text=json.dumps({'columns': []}))
        with patch('api.helpers.openai_helpers.query_with_flex_fallback', side_effect=response) as query:
            process_next_conversion(); process_next_conversion()
            self.assertTrue(all(entry['confidence'] == 'low' for entry in self.conversion.tidy['model']['entries'].values()))
            self.assertEqual(self.post('inspect').status_code, 202)
            process_next_conversion()
            self.assertFalse(DwcConversionJob.objects.filter(conversion=self.conversion).exists())
            self.assertEqual(query.call_count, 1)

    def test_a_superseded_tidy_call_is_paid_for_but_changes_nothing(self):
        conversion_id = self.conversion.pk

        def response(args, max_retries=None):
            # Meanwhile the user asks for a new inspection, which replaces the running tidy job.
            DwcConversionJob.objects.filter(conversion_id=conversion_id).delete()
            DwcConversionJob.objects.create(conversion_id=conversion_id, action='inspect')
            return SimpleNamespace(id='superseded', status='completed', model='gpt-6-sol', usage={'input_tokens': 10, 'output_tokens': 5},
                                   output_text=json.dumps({'columns': []}))
        with patch('api.helpers.openai_helpers.query_with_flex_fallback', side_effect=response):
            process_next_conversion()
            plan_id = self.conversion.plan['id']
            process_next_conversion()
        self.assertTrue(OpenAIUsage.objects.filter(response_id='superseded').exists())
        conversion = self.conversion
        self.assertEqual(conversion.plan['id'], plan_id)
        self.assertEqual(conversion.tidy['model']['status'], 'running')
        self.assertEqual(DwcConversionJob.objects.get(conversion=conversion).action, 'inspect')

    def test_model_failure_keeps_deterministic_plan_and_undo_does_not_call_model(self):
        with patch('api.helpers.openai_helpers.query_with_flex_fallback', side_effect=RuntimeError('offline')) as query:
            process_next_conversion(); process_next_conversion()
            self.assertEqual(self.conversion.status, 'review')
            self.assertEqual(self.conversion.tidy['model']['status'], 'error')
            self.assertFalse(self.conversion.conflicts)
            group = next(group for group in self.conversion.tidy['summary']['groups'] if group['rule'] == 'vocabulary')
            self.post('tidy', changes={group['id']: 'undo'})
            process_next_conversion()
            query.assert_called_once()

    def test_cost_limit_and_disabled_ai_never_block_deterministic_inspection(self):
        with override_settings(OPENAI_DATASET_COST_LIMIT_USD='0.000001'), \
                patch('api.helpers.openai_helpers.query_with_flex_fallback') as query:
            process_next_conversion()
            self.assertEqual(DwcConversionJob.objects.get(conversion=self.conversion).action, 'tidy')
            process_next_conversion()
            self.assertEqual(self.conversion.status, 'review')
            self.assertEqual(self.conversion.tidy['model']['status'], 'cost-limit')
            query.assert_not_called()

        with override_settings(CONVERSION_AI_REVIEW_ENABLED=False), \
                patch('api.helpers.openai_helpers.query_with_flex_fallback') as query:
            self.assertEqual(self.post('inspect').status_code, 202)
            process_next_conversion()
            self.assertFalse(DwcConversionJob.objects.filter(conversion=self.conversion).exists())
            self.assertEqual(self.conversion.status, 'review')
            query.assert_not_called()
