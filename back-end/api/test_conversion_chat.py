"""Conversation fallback: answer verification, confirmations, batching and the Responses tool loop (mocked)."""
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone

from api import conversion_chat as chat
from api.conversion_jobs import process_next_conversion
from api.models import Agent, DwcConversion, DwcConversionJob, DwcConversionMessage, Message
from api.test_conversion_review import AI, QUERY, ConversionTestCase, answer, reply, requested


def call(name, arguments, call_id):
    return {'type': 'function_call', 'id': f'fc_{call_id}', 'call_id': call_id, 'name': name, 'arguments': json.dumps(arguments)}


def chat_response(output=(), final=None, response_id='chat'):
    return SimpleNamespace(id=response_id, status='completed', model='gpt-6-sol', service_tier='default', usage={},
                           output=[{'type': 'reasoning', 'id': f'rs_{response_id}', 'encrypted_content': 'opaque', 'summary': []},
                                   *output],
                           output_text=json.dumps(final) if final is not None else '')


def final(message='Thanks, recorded.', asked=(), proposals=()):
    return {'message': message, 'asked': list(asked), 'proposals': list(proposals)}


@override_settings(**AI)
class ChatTests(ConversionTestCase):
    def ready(self, asked=('column:0:2',)):
        self.inspect_only()
        self.assertEqual(self.post('save', changes={'loose-links': 'confirm'}).status_code, 200)
        conversion = self.conversion
        return DwcConversionMessage.objects.create(conversion=conversion, role='assistant', kind='questions',
                                                   content='Should the scientific names be copied as written?',
                                                   asked=list(asked), plan_id=conversion.plan['id'])

    def say(self, text):
        response = self.post('chat', message=text)
        self.assertEqual(response.status_code, 202, response.data)
        return DwcConversionMessage.objects.filter(role='user').latest('id')

    def test_turn_records_asked_answers_and_turns_assertions_into_proposals(self):
        question = self.ready()
        message = self.say('Copy the names exactly as written. They are all present records.')
        self.assertEqual(DwcConversionJob.objects.get(conversion=self.conversion).action, 'chat')
        payloads = []
        responses = [
            chat_response([call('set_decision', {'id': 'status:0', 'value': 'present', 'user_message_id': message.id,
                                                 'user_quote': 'They are all present records'}, 'c1'),
                           call('set_decision', {'id': 'event-grain', 'value': 'by_id', 'user_message_id': message.id,
                                                 'user_quote': 'Copy the names'}, 'c2')], response_id='r1'),
            chat_response([call('set_decision', {'id': 'column:0:2', 'value': 'occurrence.scientificName',
                                                 'user_message_id': message.id, 'user_quote': 'copy the  NAMES exactly'}, 'c3')],
                          response_id='r2'),
            chat_response(final=final(asked=['status:0', 'bogus'], proposals=[
                {'id': 'status:0', 'value': 'present'}, {'id': 'column:0:2', 'value': 'invented'}]), response_id='r3'),
        ]

        def respond(payload, max_retries=None):
            payloads.append(json.loads(json.dumps(payload)))
            return responses[len(payloads) - 1]

        with patch(QUERY, side_effect=respond):
            process_next_conversion()
        conversion = self.conversion
        self.assertEqual(conversion.decisions, {'loose-links': 'confirm', 'column:0:2': 'occurrence.scientificName'})
        event = self.events('column:0:2')[-1]
        self.assertEqual((event.source, event.message_id, event.response_id), ('chat', message.id, 'r2'))
        self.assertEqual(event.transcript['question']['message_id'], question.id)
        # Tool results explained the refusals to the model.
        outputs = {item['call_id']: json.loads(item['output']) for item in payloads[1]['input'] if item.get('type') == 'function_call_output'}
        self.assertIn('proposals', outputs['c1']['error'])
        self.assertIn('Ask the user', outputs['c2']['error'])
        # Reasoning and function-call items are replayed within the turn.
        self.assertIn({'type': 'reasoning', 'id': 'rs_r1', 'encrypted_content': 'opaque', 'summary': []}, payloads[1]['input'])
        for payload in payloads:
            self.assertFalse(payload['store'])
            self.assertEqual(payload['include'], ['reasoning.encrypted_content'])
            self.assertFalse(payload['parallel_tool_calls'])
            self.assertEqual(payload['service_tier'], 'default')
            self.assertTrue(all(tool['strict'] for tool in payload['tools']))
            self.assertTrue(payload['text']['format']['strict'])
        reply_message = DwcConversionMessage.objects.filter(role='assistant').latest('id')
        self.assertEqual(reply_message.answers_through, message.id)
        self.assertEqual(reply_message.actions, ['column:0:2'])
        self.assertEqual(reply_message.asked, ['status:0'])
        self.assertEqual(reply_message.proposals, [{'id': 'status:0', 'value': 'present'}])
        # Items no review has seen yet are reviewed automatically after the turn.
        self.assertEqual(DwcConversionJob.objects.get(conversion=conversion).action, 'review')
        self.assertFalse(self.client.get(self.url).data['chat']['pending'])
        # The assertion is applied only by the user's click on that exact proposal.
        stale = self.post('chat', confirm=[{'message_id': question.id, 'id': 'status:0', 'value': 'present'}])
        self.assertEqual(stale.status_code, 400)
        other = self.post('chat', confirm=[{'message_id': reply_message.id, 'id': 'status:0', 'value': 'absent'}])
        self.assertEqual(other.status_code, 400)
        confirmed = self.post('chat', confirm=[{'message_id': reply_message.id, 'id': 'status:0', 'value': 'present'}])
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        event = self.events('status:0')[-1]
        self.assertEqual((event.source, event.evidence), ('chat', [{'confirmed': True}]))
        self.assertEqual(event.message.kind, 'confirmation')
        self.assertFalse(confirmed.data['chat']['pending'])
        self.assertFalse(Agent.objects.exists()); self.assertFalse(Message.objects.exists())

    def test_quotes_batches_and_one_answer_per_batch(self):
        self.ready()
        first = self.say('Keep the names in the original files only.')
        conversion = self.conversion
        now = timezone.now()
        job = DwcConversionJob.objects.get(conversion=conversion)
        DwcConversionJob.objects.filter(pk=job.pk).update(claimed_at=now)
        session = chat.Session(conversion.pk, job.pk, now, conversion.plan['id'], [first.id], True)
        self.assertIn('quote', session.set_decision('column:0:2', 'preserve', first.id, 'invented words')['error'])
        self.assertIn('not one of', session.set_decision('column:0:2', 'preserve', first.id + 99, 'Keep')['error'])
        self.assertTrue(session.set_decision('column:0:2', 'preserve', first.id, 'original files only')['ok'])
        self.assertTrue(session.set_decision('column:0:2', 'preserve', first.id, 'original files only')['ok'])
        changed = session.set_decision('column:0:2', 'occurrence.scientificName', first.id, 'Keep the names')
        self.assertIn('already answered', changed['error'])
        self.assertEqual(self.conversion.decisions['column:0:2'], 'preserve')
        # A superseded worker cannot write.
        DwcConversionJob.objects.filter(pk=job.pk).delete()
        with self.assertRaises(chat.review.Fenced):
            session.set_decision('column:0:2', 'occurrence.scientificName', first.id, 'Keep the names')

    def test_messages_during_review_are_answered_together_after_it(self):
        self.inspect_only()
        conversion = self.conversion
        DwcConversionJob.objects.create(conversion=conversion, action='review')
        DwcConversion.objects.filter(pk=conversion.pk).update(status='reviewing')
        first, second = self.say('Hello'), self.say('Are you there?')
        self.assertEqual(DwcConversionJob.objects.get(conversion=conversion).action, 'review')

        def respond(payload, max_retries=None):
            if 'tools' not in payload:
                return reply([answer(item_id, 'abstain') for item_id in requested(payload)])
            self.assertIn(f'[message {first.id}] Hello', json.dumps(payload['input']))
            return chat_response(final=final('Yes, here.'))

        with patch(QUERY, side_effect=respond):
            process_next_conversion()
            self.assertEqual(DwcConversionJob.objects.get(conversion=conversion).action, 'chat')
            self.assertEqual(self.conversion.status, 'review')
            process_next_conversion()
        replies = DwcConversionMessage.objects.filter(role='assistant', kind='')
        self.assertEqual([item.answers_through for item in replies], [second.id])
        self.assertFalse(DwcConversionJob.objects.filter(conversion=conversion).exists())

    def test_invalid_reply_is_retried_once_then_answered_with_a_notice(self):
        self.ready()
        message = self.say('Hi')
        broken = SimpleNamespace(id='bad', status='completed', model='gpt-6-sol', service_tier='default', usage={}, output=[],
                                 output_text='not json')
        with patch(QUERY, return_value=broken) as query:
            process_next_conversion()
        self.assertEqual(query.call_count, 2)
        notice = DwcConversionMessage.objects.filter(role='assistant').latest('id')
        self.assertEqual((notice.kind, notice.content, notice.answers_through), ('notice', chat.ERROR_NOTICE, message.id))
        self.assertNotEqual(DwcConversionJob.objects.get(conversion=self.conversion).action, 'chat')

    def test_blocked_conversations_explain_without_decision_tools(self):
        self.ready()
        DwcConversion.objects.filter(pk=self.conversion.pk).update(status='blocked')
        self.say('What do I fix?')
        with patch(QUERY, return_value=chat_response(final=final('Correct the file.'))) as query:
            process_next_conversion()
        self.assertNotIn('set_decision', [tool['name'] for tool in query.call_args.args[0]['tools']])
        self.assertEqual(self.post('chat', confirm=[{'message_id': 1, 'id': 'status:0', 'value': 'present'}]).status_code, 400)
        DwcConversion.objects.filter(pk=self.conversion.pk).update(status='complete')
        self.assertEqual(self.post('chat', message='Hello').status_code, 409)

    def test_cost_limit_answers_with_a_notice_and_no_call(self):
        self.ready()
        message = self.say('Hi')
        with override_settings(OPENAI_DATASET_COST_LIMIT_USD='0.0001'), patch(QUERY) as query:
            process_next_conversion()
        query.assert_not_called()
        notice = DwcConversionMessage.objects.filter(role='assistant').latest('id')
        self.assertEqual((notice.content, notice.answers_through), (chat.COST_LIMIT_NOTICE, message.id))

    def test_conversation_requires_ai(self):
        self.ready()
        with override_settings(CONVERSION_AI_REVIEW_ENABLED=False):
            self.assertEqual(self.post('chat', message='Hi').status_code, 400)

    def test_convert_supersedes_a_pending_chat_and_acknowledges_it(self):
        self.ready()
        message = self.say('Hi')
        conversion = self.conversion
        decisions = {issue['id']: issue['options'][0]['value'] for issue in conversion.plan['issues']}
        self.assertEqual(self.post('save', changes={key: value for key, value in decisions.items() if key != 'loose-links'}).status_code, 200)
        self.assertEqual(self.post('convert').status_code, 202)
        self.assertEqual(DwcConversionJob.objects.get(conversion=conversion).action, 'convert')
        notice = DwcConversionMessage.objects.filter(role='assistant').latest('id')
        self.assertEqual((notice.content, notice.answers_through), (chat.SUPERSEDED_NOTICE, message.id))
