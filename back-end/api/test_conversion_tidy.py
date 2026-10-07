"""Reviewable tidy-up in conversion planning, re-planning, audit and reporting."""
import io
import tarfile
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from api import conversion_review, conversion_tidy
from api.conversion_jobs import load_sources
from api.conversion_jobs import process_next_conversion
from api.dwca_semantic_audit import is_age_like_remark
from api.dwca_value_ledger import build_value_disposition_ledger
from api.models import DwcConversionDecisionEvent, DwcConversionJob, DwcConversionMessage, Table
from api.test_conversion_review import ConversionTestCase


OCCURRENCE = (
    b'occurrenceID,basisOfRecord,countryCode,sex,lifeStage,eventRemarks,scientificName,occurrenceStatus,organismQuantity\n'
    b'o1,HumanObservation,Norway,f,,ad,Aus bus,present,"1,"\n'
    b'o2,HumanObservation,Great Britain,M,,juv.,Aus bus,present,"2,"\n'
    b'o3,HumanObservation,NO,Female,,1 juv.,Cus dus,present,"3,"\n'
)


class TidyFlowTests(ConversionTestCase):
    files = [('occurrence.csv', OCCURRENCE)]

    def test_inspect_reports_tidy_values_and_can_undo_with_plan_state_carried(self):
        with override_settings(CONVERSION_AI_REVIEW_ENABLED=False, CONVERSION_NAME_CHECKS_ENABLED=False):
            process_next_conversion()
        conversion = self.conversion
        self.assertTrue(conversion.tidy['summary']['groups'])
        self.assertIn('tidy', conversion.plan)
        # Settled labels and remarks are not asked about; '1 juv.' is left for a person (or the model layer).
        self.assertEqual([item['source_value'] for item in conversion.plan['issues']
                          if item['id'].startswith(('country-label:', 'age-remark:'))], ['1 juv.'])
        country_column = next(item for item in conversion.plan['columns'] if item['term'].endswith('/country'))
        self.assertIn('tidy_added', country_column)
        country_code = next(item for item in conversion.plan['columns'] if item['term'].endswith('/countryCode'))
        self.assertEqual(country_code['samples'], ['NO', 'GB'])
        state = self.client.get(self.url).data['tidy']
        self.assertTrue(state['groups'])
        self.assertIn('tidied_groups', state['counts'])

        # A user choice and its provenance survive a tidy-only plan change.
        choice = next(item for item in conversion.plan['columns'] if item['id'] == 'column:0:1')
        conversion_review.apply_decision_changes(conversion, {choice['id']: choice['default']}, 'user')
        conversion.save()
        old_plan = conversion.plan['id']
        message = DwcConversionMessage.objects.create(conversion=conversion, role='user', content='Keep going', plan_id=old_plan)
        group = next(item for item in conversion.tidy['summary']['groups'] if item['rule'] == 'country-name')
        response = self.post('tidy', changes={group['id']: 'undo'})
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(DwcConversionJob.objects.get(conversion=conversion).action, 'replan')
        process_next_conversion()
        conversion.refresh_from_db()
        self.assertEqual(conversion.status, 'review')
        self.assertNotEqual(conversion.plan['id'], old_plan)
        self.assertEqual(conversion.decisions[choice['id']], choice['default'])
        self.assertFalse(next(item for item in conversion.tidy['summary']['groups'] if item['id'] == group['id'])['applied'])
        country_code = next(item for item in conversion.plan['columns'] if item['term'].endswith('/countryCode'))
        self.assertEqual(country_code['samples'], ['Norway', 'Great Britain', 'NO'])
        # Undone country names are asked about again, one question per label.
        self.assertEqual(sorted(item['source_value'] for item in conversion.plan['issues'] if item['id'].startswith('country-label:')),
                         ['Great Britain', 'Norway'])
        event = DwcConversionDecisionEvent.objects.filter(conversion=conversion, plan_id=conversion.plan['id'],
                                                          decision_id=choice['id']).latest('id')
        self.assertEqual(event.transcript, {'carried_from_plan': old_plan})
        message.refresh_from_db()
        self.assertEqual(message.plan_id, conversion.plan['id'])
        # A conversation about a question the undo changed stays with the plan it was about.
        asked = DwcConversionMessage.objects.create(conversion=conversion, role='assistant', content='Where does countryCode go?',
                                                    plan_id=conversion.plan['id'], asked=['column:0:2'])
        redo_from = conversion.plan['id']
        self.assertEqual(self.post('tidy', changes={group['id']: None}).status_code, 202)
        process_next_conversion()
        conversion.refresh_from_db()
        asked.refresh_from_db(); message.refresh_from_db()
        self.assertNotEqual(conversion.plan['id'], redo_from)
        self.assertEqual((asked.plan_id, message.plan_id), (redo_from, redo_from))

        suggestion = next(item for item in conversion.tidy['summary']['groups'] if item['rule'] == 'trailing-separator')
        value_id = suggestion['values'][0]['id']
        response = self.post('tidy', changes={value_id: 'apply'})
        self.assertEqual(response.status_code, 202, response.data)
        process_next_conversion()
        conversion.refresh_from_db()
        suggestion = next(item for item in conversion.tidy['summary']['groups'] if item['id'] == suggestion['id'])
        self.assertTrue(suggestion['values'][0]['applied'])

    def test_replan_failure_keeps_old_plan_and_drops_pending_override(self):
        with override_settings(CONVERSION_AI_REVIEW_ENABLED=False, CONVERSION_NAME_CHECKS_ENABLED=False):
            process_next_conversion()
        conversion = self.conversion
        old_plan = conversion.plan
        group = next(item for item in conversion.tidy['summary']['groups'] if item['rule'] == 'country-name')
        self.post('tidy', changes={group['id']: 'undo'})
        with patch('api.conversion_jobs.build_plan', side_effect=RuntimeError('one-off')):
            process_next_conversion()
        conversion.refresh_from_db()
        self.assertEqual(conversion.status, 'review')
        self.assertEqual(conversion.plan, old_plan)
        self.assertNotIn('pending_overrides', conversion.tidy)

    def test_bad_tidy_requests_and_wrong_plan_are_rejected(self):
        with override_settings(CONVERSION_AI_REVIEW_ENABLED=False, CONVERSION_NAME_CHECKS_ENABLED=False):
            process_next_conversion()
        self.assertEqual(self.post('tidy', changes={'no-such-id': 'undo'}).status_code, 400)
        identifier = self.conversion.tidy['summary']['groups'][0]['id']
        self.assertEqual(self.post('tidy', changes={identifier: 'maybe'}).status_code, 400)
        response = self.client.post(self.url, {'action': 'tidy', 'plan_id': 'old',
                                               'changes': {identifier: 'undo'}}, format='json')
        self.assertEqual(response.status_code, 409)
        conversion = self.conversion
        conversion.status = 'complete'
        conversion.save(update_fields=['status'])
        self.assertEqual(self.post('tidy', changes={identifier: 'undo'}).status_code, 409)

    @override_settings(CONVERSION_TIDY_ENABLED=False, CONVERSION_AI_REVIEW_ENABLED=False,
                       CONVERSION_NAME_CHECKS_ENABLED=False)
    def test_disabled_tidy_keeps_legacy_value_questions(self):
        process_next_conversion()
        conversion = self.conversion
        self.assertFalse(conversion.tidy)
        self.assertTrue(any(item['id'].startswith('country-label:') for item in conversion.plan['issues']))
        self.assertTrue(any(item['id'].startswith('age-remark:') for item in conversion.plan['issues']))

    def test_conversion_report_and_originals_keep_tidy_provenance_and_source_bytes(self):
        with override_settings(CONVERSION_AI_REVIEW_ENABLED=False, CONVERSION_NAME_CHECKS_ENABLED=False):
            process_next_conversion()
        conversion = self.conversion
        decisions = {item['id']: item['default'] for item in conversion.plan['columns']
                     if item['default'] in {option['value'] for option in item['options']}}
        for item in conversion.plan['issues']:
            if item.get('options'):
                decisions[item['id']] = item['options'][0]['value']
        response = self.post('convert', decisions=decisions)
        self.assertEqual(response.status_code, 202, response.data)
        with override_settings(CONVERSION_AI_REVIEW_ENABLED=False, CONVERSION_NAME_CHECKS_ENABLED=False):
            process_next_conversion()
        conversion = self.conversion
        self.assertEqual(conversion.status, 'complete', conversion.error)
        self.assertTrue(conversion.report['validation']['valid'])
        tidy_report = conversion.report['tidy']
        self.assertTrue(any(group['rule'] == 'country-name' and group['applied'] for group in tidy_report['groups']))
        sex = next(item for item in conversion.report['value_disposition']['source_terms']
                   if item['source_term'].endswith('/sex'))
        self.assertGreater(sex['tidied_values'], 0)
        self.assertTrue(any(item['source_term'].endswith('/eventRemarks') and item['tidy_cleared_values']
                            for item in conversion.report['value_disposition']['source_terms']))
        occurrence = Table.objects.get(dataset=conversion.dataset, title='occurrence').df
        self.assertEqual(list(occurrence['sex']), ['female', 'male', 'female'])
        self.assertEqual(list(occurrence['lifeStage']), ['adult', 'juvenile', ''])
        event = Table.objects.get(dataset=conversion.dataset, title='event').df
        self.assertEqual(list(event['countryCode']), ['NO', 'GB', 'NO'])
        with conversion.output_file.open('rb') as stream, tarfile.open(fileobj=io.BytesIO(stream.read()), mode='r:gz') as archive:
            originals = zipfile.ZipFile(io.BytesIO(archive.extractfile('source-originals.zip').read()))
            self.assertEqual(originals.read('occurrence.csv'), OCCURRENCE)
            report = archive.extractfile('conversion-report.json').read().decode('utf-8')
            self.assertIn('"tidy"', report)

    def test_reinspect_keeps_same_source_overrides(self):
        with override_settings(CONVERSION_AI_REVIEW_ENABLED=False, CONVERSION_NAME_CHECKS_ENABLED=False):
            process_next_conversion()
        conversion = self.conversion
        group = next(item for item in conversion.tidy['summary']['groups'] if item['rule'] == 'country-name')
        self.post('tidy', changes={group['id']: 'undo'})
        process_next_conversion()
        with override_settings(CONVERSION_AI_REVIEW_ENABLED=False, CONVERSION_NAME_CHECKS_ENABLED=False):
            response = self.post('inspect')
            self.assertEqual(response.status_code, 202)
            process_next_conversion()
        conversion = self.conversion
        self.assertEqual(conversion.tidy['overrides'][group['id']], 'off')


class TidyGroupApplyTests(ConversionTestCase):
    # 31 ambiguous values: more than the page lists, all applied with the group's "Apply all".
    files = [('occurrence.csv', b'occurrenceID,organismQuantity,organismQuantityType,occurrenceStatus\n'
              + b''.join(f'o{n},"{n},",individuals,present\n'.encode() for n in range(1, 32)))]

    def test_apply_all_reaches_values_beyond_the_summary(self):
        with override_settings(CONVERSION_AI_REVIEW_ENABLED=False, CONVERSION_NAME_CHECKS_ENABLED=False):
            process_next_conversion()
        group = next(item for item in self.conversion.tidy['summary']['groups'] if item['rule'] == 'trailing-separator')
        self.assertEqual((len(group['values']), group['more_values']), (30, 1))
        self.assertEqual(self.post('tidy', changes={group['id']: 'apply'}).status_code, 202)
        with override_settings(CONVERSION_AI_REVIEW_ENABLED=False, CONVERSION_NAME_CHECKS_ENABLED=False):
            process_next_conversion()
        column = next(item for item in self.conversion.plan['columns'] if item['term'].endswith('/organismQuantity'))
        self.assertEqual(column['samples'], ['1', '2', '3'])
        archive = conversion_tidy.tidied(self.conversion, load_sources(self.conversion, tidy=False))
        self.assertEqual({row[1] for row in archive.tables[0].rows}, {str(n) for n in range(1, 32)})


class TidyUnitTests(SimpleTestCase):
    def test_group_actions_replace_single_value_choices(self):
        group = 'tidy:0:7:thousands-or-decimal'
        conversion = SimpleNamespace(tidy={'overrides': {group + ':aaaa': 'on', 'tidy:0:8:vocabulary': 'off'},
                                           'summary': {'groups': [{'id': group, 'values': [{'id': group + ':aaaa'}]}]}})
        self.assertEqual(conversion_tidy.request_changes(conversion, {group: 'undo'}), {group: 'off', 'tidy:0:8:vocabulary': 'off'})

    def test_report_lists_every_value_except_long_space_only_groups(self):
        values = [{'value': f'a  {n}', 'fields': {'locality': f'a {n}'}} for n in range(conversion_tidy.REPORT_WHITESPACE_VALUES + 5)]
        view = SimpleNamespace(tidy={'version': '1', 'sha256': 'x', 'added_columns': [], 'groups': [
            {'rule': 'whitespace', 'values': values}, {'rule': 'vocabulary', 'values': values}]})
        report = conversion_tidy.report_section(SimpleNamespace(tidy={}), view)
        self.assertEqual((len(report['groups'][0]['values']), report['groups'][0]['more_values']),
                         (conversion_tidy.REPORT_WHITESPACE_VALUES, 5))
        self.assertEqual(len(report['groups'][1]['values']), len(values))

    def test_age_remark_vocabulary(self):
        for value in ('ad', 'juv.', '1 juv.', 'adult + egg', 'ad.m.egg'):
            self.assertTrue(is_age_like_remark(value), value)
        for value in ('fad', 'sampled by hand'):
            self.assertFalse(is_age_like_remark(value), value)

    def test_ledger_counts_rewrites_and_cleared_values(self):
        plan = {'tables': [{'name': 'occurrence', 'index': 0}], 'columns': [
            {'id': 'column:0:0', 'table': 0, 'column': 0, 'term': 'http://rs.tdwg.org/dwc/terms/sex',
             'nonempty': 2, 'default': 'occurrence.sex'}]}
        report = {'columns': [], 'tidy': {'groups': [{
            'table': 0, 'column': 0, 'field': 'sex', 'applied': True, 'tidied_rows': 1, 'cleared_rows': 1,
            'values': [{'applied': True, 'changed_rows': 1, 'fields': {'sex': 'female'}}]}]}}
        entry = build_value_disposition_ledger(plan, report)['source_terms'][0]
        self.assertEqual(entry['tidied_values'], 1)
        self.assertEqual(entry['tidy_cleared_values'], 1)
        self.assertEqual(entry['source_nonempty_values'], 3)
