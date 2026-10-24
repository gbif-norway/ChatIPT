import json
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from api import agent_tools, gbif_pause
from api.agent_turns import _retry_or_fail_job, process_next_agent_turn, queue_agent_turn
from api.conversion_jobs import process_next_conversion
from api.models import (
    Agent, AgentTurnJob, CustomUser, Dataset, DwcConversion, DwcConversionJob, GbifOutage, Message, Task,
)
from api.quality_report import prepare_gate_report, refresh_refinement_report


def outage(started_hours_ago, ended_hours_ago=None):
    now = timezone.now()
    return GbifOutage.objects.create(
        started_at=now - timedelta(hours=started_hours_ago),
        ended_at=None if ended_hours_ago is None else now - timedelta(hours=ended_hours_ago),
    )


class GbifPauseSwitchTests(TestCase):
    def test_start_keeps_one_open_outage_and_end_closes_it(self):
        first, created = gbif_pause.start('Validator stuck.', by='admin')
        again, created_again = gbif_pause.start(by='someone else')

        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(again.pk, first.pk)
        self.assertEqual(gbif_pause.status()['message'], 'Validator stuck.')

        ended = gbif_pause.end(by='admin')

        self.assertEqual(ended.pk, first.pk)
        self.assertIsNotNone(ended.ended_at)
        self.assertEqual(gbif_pause.status(), {'paused': False})
        self.assertIsNone(gbif_pause.end())

    def test_default_notice_is_shown_without_a_message(self):
        gbif_pause.start()

        self.assertEqual(gbif_pause.status()['message'], gbif_pause.DEFAULT_NOTICE)

    def test_paused_time_is_left_out_of_elapsed_time(self):
        outage(started_hours_ago=5, ended_hours_ago=4)  # before the wait: ignored
        outage(started_hours_ago=3, ended_hours_ago=2)  # inside the wait: 1 hour
        outage(started_hours_ago=0.5)  # still open: 0.5 hours so far

        active = gbif_pause.active_seconds_since(timezone.now() - timedelta(hours=3.5))

        self.assertAlmostEqual(active, 2 * 3600, delta=5)

    def test_admin_end_action_needs_the_ongoing_outage_selected(self):
        from django.contrib.admin.sites import site
        from django.test import RequestFactory

        admin_view = site._registry[GbifOutage]
        old = outage(started_hours_ago=5, ended_hours_ago=4)
        gbif_pause.start()
        request = RequestFactory().post('/')
        request.user = CustomUser.objects.create_superuser(username='admin', password='x')
        with patch.object(admin_view, 'message_user'):
            admin_view.end_outage(request, GbifOutage.objects.filter(pk=old.pk))
            self.assertTrue(gbif_pause.is_paused())

            admin_view.end_outage(request, GbifOutage.objects.all())
        self.assertFalse(gbif_pause.is_paused())

    def test_management_command_switches_the_pause(self):
        out = StringIO()
        call_command('gbif_pause', 'on', '--message', 'Validator stuck.', stdout=out)
        self.assertTrue(gbif_pause.is_paused())
        self.assertEqual(GbifOutage.objects.get().started_by, 'manage.py')

        call_command('gbif_pause', 'off', stdout=out)
        self.assertFalse(gbif_pause.is_paused())
        self.assertIn('Resumed', out.getvalue())


class GbifPauseWorkerTests(TestCase):
    def setUp(self):
        close_connections = patch('api.agent_turns.close_old_connections')
        close_connections.start()
        self.addCleanup(close_connections.stop)
        self.user = CustomUser.objects.create_user(username='pause-user', password='test')
        self.dataset = Dataset.objects.create(user=self.user, title='Paused workflow')
        self.task = Task.objects.create(name='Data transformation', text='Transform', order=1)
        self.agent = Agent.objects.create(dataset=self.dataset, task=self.task)
        self.message = Message.objects.create(agent=self.agent, openai_obj={'role': 'user', 'content': 'Continue'})

    def test_queued_turn_waits_for_the_outage_to_end(self):
        queue_agent_turn(self.agent)
        gbif_pause.start()

        with patch.object(Agent, 'next_message') as next_message:
            self.assertFalse(process_next_agent_turn())
        next_message.assert_not_called()
        job = AgentTurnJob.objects.get()
        self.assertIsNone(job.claimed_at)
        self.assertEqual(job.failure_count, 0)

        gbif_pause.end()

        def answer(agent):
            Message.objects.create(agent=agent, openai_obj={'role': 'assistant', 'content': 'Done.'})

        with patch.object(Agent, 'next_message', answer):
            self.assertTrue(process_next_agent_turn())
        self.assertFalse(AgentTurnJob.objects.exists())

    def test_agent_starts_no_model_turn_while_paused(self):
        gbif_pause.start()

        with patch('api.models.create_response_message') as model:
            self.assertEqual(self.agent.next_message(), self.message)
        model.assert_not_called()
        self.assertFalse(Agent.objects.get(pk=self.agent.pk).busy_thinking)

    @patch('api.models.create_response_message')
    @patch('api.agent_tools.get_gbif_validation', return_value={'status': 'RUNNING'})
    def test_turn_claimed_before_the_pause_makes_no_model_call_after_its_gbif_recheck(self, _get, model):
        self.dataset.dwca_url = 'https://example.org/a.zip'
        self.dataset.dwca_validation = {'url': self.dataset.dwca_url, 'key': 'k1', 'status': 'RUNNING'}
        self.dataset.save(update_fields=['dwca_url', 'dwca_validation'])
        waiting = Message.objects.create(agent=self.agent, openai_obj={'role': 'tool', 'tool_call_id': 'c1', 'content': json.dumps({
            'status': 'RUNNING', 'validation_key': 'k1', 'url': self.dataset.dwca_url,
            'waiting_since': timezone.now().isoformat(),
            'next_recheck_at': (timezone.now() - timedelta(seconds=1)).isoformat(),
        })})
        real_is_paused = gbif_pause.is_paused
        checks = iter([False])  # not paused at the claim, paused by the time the recheck returns
        with patch('api.gbif_pause.is_paused', side_effect=lambda: next(checks, True)):
            self.assertEqual(self.agent.next_message(), waiting)
        model.assert_not_called()
        self.assertFalse(real_is_paused())

    @patch('api.agent_tools.get_gbif_validation', side_effect=ConnectionError('GBIF down'))
    def test_failed_validation_check_during_an_outage_keeps_waiting(self, _get):
        self.dataset.dwca_url = 'https://example.org/a.zip'
        self.dataset.dwca_validation = {
            'url': self.dataset.dwca_url, 'key': 'k2', 'status': 'RUNNING', 'aborted_keys': ['k1'],
        }
        self.dataset.save(update_fields=['dwca_url', 'dwca_validation'])
        gbif_pause.start()

        result = json.loads(agent_tools.refresh_gbif_validation(self.dataset, {
            'validation_key': 'k2', 'url': self.dataset.dwca_url, 'failed_checks': 4,
            'waiting_since': timezone.now().isoformat(),
        }))

        self.assertEqual((result['status'], result['failed_checks']), ('RUNNING', 4))
        self.assertEqual(result['aborted_keys'], ['k1'])

    @patch('api.quality_report._delete_archive')
    @patch('api.quality_report.agent_tools.get_gbif_validation', side_effect=ConnectionError('GBIF down'))
    def test_failed_provisional_check_during_an_outage_keeps_waiting(self, _get, delete_mock):
        from api.quality_report import _refinement_waiting_payload

        record = {'agent_id': self.agent.id, 'url': 'https://example.org/p.zip', 'key': 'p1', 'status': 'RUNNING',
                  'submitted_at': timezone.now().isoformat()}
        self.dataset.provisional_dwca_validation = record
        self.dataset.save(update_fields=['provisional_dwca_validation'])
        gbif_pause.start()

        result = json.loads(refresh_refinement_report(self.dataset, json.loads(_refinement_waiting_payload(record, 4))))

        self.assertEqual((result['status'], result['failed_checks']), ('RUNNING', 4))
        delete_mock.assert_not_called()

    @patch('api.agent_tools.submit_gbif_validation', side_effect=ConnectionError('GBIF down'))
    @patch('api.agent_tools.get_gbif_validation', return_value={'status': 'ABORTED'})
    def test_failed_resubmission_during_an_outage_keeps_waiting(self, _get, _submit):
        self.dataset.dwca_url = 'https://example.org/a.zip'
        self.dataset.dwca_validation = {'url': self.dataset.dwca_url, 'key': 'k1', 'status': 'RUNNING'}
        self.dataset.save(update_fields=['dwca_url', 'dwca_validation'])
        gbif_pause.start()

        result = json.loads(agent_tools.refresh_gbif_validation(self.dataset, {
            'validation_key': 'k1', 'url': self.dataset.dwca_url, 'waiting_since': timezone.now().isoformat(),
        }))

        self.assertEqual((result['status'], result['validation_key']), ('RUNNING', 'k1'))

    @patch('api.quality_report._delete_archive')
    @patch('api.quality_report.agent_tools.submit_gbif_validation', side_effect=ConnectionError('GBIF down'))
    @patch('api.quality_report.agent_tools.get_gbif_validation', return_value={'status': 'ABORTED'})
    def test_failed_provisional_resubmission_during_an_outage_keeps_waiting(self, _get, _submit, delete_mock):
        from api.quality_report import _refinement_waiting_payload

        record = {'agent_id': self.agent.id, 'url': 'https://example.org/p.zip', 'key': 'p1', 'status': 'RUNNING',
                  'submitted_at': timezone.now().isoformat()}
        self.dataset.provisional_dwca_validation = record
        self.dataset.save(update_fields=['provisional_dwca_validation'])
        gbif_pause.start()

        result = json.loads(refresh_refinement_report(self.dataset, json.loads(_refinement_waiting_payload(record))))

        self.assertEqual((result['status'], result['validation_key']), ('RUNNING', 'p1'))
        delete_mock.assert_not_called()

    def test_failure_during_an_outage_is_not_counted(self):
        queue_agent_turn(self.agent)
        claimed_at = timezone.now()
        AgentTurnJob.objects.update(claimed_at=claimed_at)
        gbif_pause.start()

        _retry_or_fail_job(AgentTurnJob.objects.get().pk, claimed_at)

        job = AgentTurnJob.objects.get()
        self.assertEqual(job.failure_count, 0)
        self.assertIsNone(job.claimed_at)

    def test_conversion_job_is_not_claimed_while_paused(self):
        dataset = Dataset.objects.create(user=self.user, workflow_type=Dataset.WorkflowType.DWCA_CONVERSION)
        conversion = DwcConversion.objects.create(dataset=dataset)
        DwcConversionJob.objects.create(conversion=conversion, action='inspect')
        gbif_pause.start()

        self.assertFalse(process_next_conversion())

        self.assertIsNone(DwcConversionJob.objects.get().claimed_at)


class GbifPauseValidationClockTests(TestCase):
    def setUp(self):
        self.task = Task.objects.create(name=Task.PREPUBLICATION_QUALITY_TASK, text='Check', order=1)
        self.dataset = Dataset.objects.create(title='Gate', description='Test', dwca_url='https://example.org/a.zip')
        # GBIF never left the queue: 4 hours of waiting, 2 of them paused.
        outage(started_hours_ago=3, ended_hours_ago=1)
        self.since = (timezone.now() - timedelta(hours=4)).isoformat()

    @patch('api.agent_tools.get_gbif_validation', return_value={'status': 'RUNNING'})
    def test_tool_wait_does_not_time_out_on_paused_time(self, _get):
        self.dataset.dwca_validation = {'url': self.dataset.dwca_url, 'key': 'k1', 'status': 'RUNNING'}
        self.dataset.save(update_fields=['dwca_validation'])

        result = json.loads(agent_tools.refresh_gbif_validation(self.dataset, {
            'validation_key': 'k1', 'url': self.dataset.dwca_url, 'waiting_since': self.since,
        }))

        self.assertEqual(result['status'], 'RUNNING')

    @patch('api.quality_report.agent_tools.inspect_publication_artifacts', return_value={'preflight': {'valid': True}})
    @patch('api.agent_tools.get_gbif_validation', return_value={'status': 'RUNNING'})
    def test_gate_keeps_waiting_through_paused_time(self, _get, _inspect):
        agent = Agent.create_with_system_message(dataset=self.dataset, task=self.task, tables=[])
        self.dataset.dwca_validation = {
            'url': self.dataset.dwca_url, 'key': 'k1', 'status': 'RUNNING', 'submitted_at': self.since,
        }
        self.dataset.save(update_fields=['dwca_validation'])

        self.assertFalse(prepare_gate_report(agent))

    @patch('api.quality_report.agent_tools.inspect_publication_artifacts', return_value={'preflight': {'valid': True}})
    @patch('api.quality_report.agent_tools.submit_gbif_validation', side_effect=ConnectionError('GBIF down'))
    @patch('api.agent_tools.get_gbif_validation', return_value={'status': 'ABORTED'})
    def test_gate_waits_when_resubmission_fails_during_an_outage(self, _get, _submit, _inspect):
        agent = Agent.create_with_system_message(dataset=self.dataset, task=self.task, tables=[])
        self.dataset.dwca_validation = {
            'url': self.dataset.dwca_url, 'key': 'k1', 'status': 'RUNNING', 'submitted_at': self.since,
        }
        self.dataset.save(update_fields=['dwca_validation'])
        gbif_pause.start()

        self.assertFalse(prepare_gate_report(agent))
        self.assertFalse(agent.message_set.filter(openai_obj__quality_report=True).exists())

    @patch('api.quality_report._delete_archive')
    @patch('api.quality_report.agent_tools.get_gbif_validation', return_value={'status': 'RUNNING'})
    def test_provisional_wait_does_not_time_out_on_paused_time(self, _get, delete_mock):
        from api.quality_report import _refinement_waiting_payload

        record = {'agent_id': 1, 'url': 'https://example.org/p.zip', 'key': 'k1', 'status': 'RUNNING',
                  'submitted_at': self.since}
        self.dataset.provisional_dwca_validation = record
        self.dataset.save(update_fields=['provisional_dwca_validation'])

        result = json.loads(refresh_refinement_report(self.dataset, json.loads(_refinement_waiting_payload(record))))

        self.assertEqual(result['status'], 'RUNNING')
        delete_mock.assert_not_called()


class GbifQueuedAlertTests(TestCase):
    def setUp(self):
        self.dataset = Dataset.objects.create(title='Queued', dwca_url='https://example.org/a.zip')
        self.dataset.dwca_validation = {'url': self.dataset.dwca_url, 'key': 'k1', 'status': 'RUNNING'}
        self.dataset.save(update_fields=['dwca_validation'])

    @patch('api.agent_tools.discord_bot.send_discord_message')
    @patch('api.agent_tools.get_gbif_validation', return_value={'status': 'QUEUED'})
    def test_long_queue_alerts_developers_once(self, _get, discord):
        agent_tools._fetch_gbif_validation(self.dataset, 'k1', self.dataset.dwca_url)
        self.dataset.refresh_from_db()
        self.assertIn('queued_since', self.dataset.dwca_validation)
        discord.assert_not_called()

        self.dataset.dwca_validation['queued_since'] = (timezone.now() - timedelta(minutes=31)).isoformat()
        self.dataset.save(update_fields=['dwca_validation'])
        agent_tools._fetch_gbif_validation(self.dataset, 'k1', self.dataset.dwca_url)
        agent_tools._fetch_gbif_validation(self.dataset, 'k1', self.dataset.dwca_url)

        discord.assert_called_once()
        self.assertIn(f"validation k1 of dataset {self.dataset.id}'s DwC-A QUEUED for 31 minutes", discord.call_args.args[0])

    @patch('api.agent_tools.discord_bot.send_discord_message', return_value=False)
    @patch('api.agent_tools.get_gbif_validation', return_value={'status': 'QUEUED'})
    def test_undelivered_alert_is_retried(self, _get, discord):
        self.dataset.dwca_validation['queued_since'] = (timezone.now() - timedelta(minutes=31)).isoformat()
        self.dataset.save(update_fields=['dwca_validation'])

        agent_tools._fetch_gbif_validation(self.dataset, 'k1', self.dataset.dwca_url)
        agent_tools._fetch_gbif_validation(self.dataset, 'k1', self.dataset.dwca_url)

        self.assertEqual(discord.call_count, 2)
        self.assertFalse(self.dataset.dwca_validation.get('queued_alert_sent'))

    @patch('api.helpers.discord_bot.requests.post', side_effect=__import__('requests').ConnectionError('down'))
    def test_discord_errors_do_not_raise(self, _post):
        from api.helpers import discord_bot

        with patch.dict('os.environ', {'DISCORD_WEBHOOK': 'https://discord.example/hook'}), \
                self.settings(TESTING=False):
            self.assertFalse(discord_bot.send_discord_message('hello'))

    @patch('api.agent_tools.discord_bot.send_discord_message')
    @patch('api.agent_tools.get_gbif_validation', return_value={'status': 'RUNNING'})
    def test_leaving_the_queue_clears_the_queue_clock(self, _get, discord):
        self.dataset.dwca_validation['queued_since'] = (timezone.now() - timedelta(minutes=31)).isoformat()
        self.dataset.save(update_fields=['dwca_validation'])

        validation = agent_tools._fetch_gbif_validation(self.dataset, 'k1', self.dataset.dwca_url)

        self.assertNotIn('queued_since', validation)
        discord.assert_not_called()

    @patch('api.agent_tools.discord_bot.send_discord_message')
    @patch('api.quality_report.agent_tools.get_gbif_validation', return_value={'status': 'QUEUED'})
    def test_provisional_queue_is_saved_and_alerted(self, _get, discord):
        from api.quality_report import _refinement_waiting_payload

        record = {'agent_id': 1, 'url': 'https://example.org/p.zip', 'key': 'p1', 'status': 'RUNNING',
                  'submitted_at': timezone.now().isoformat()}
        self.dataset.provisional_dwca_validation = record
        self.dataset.save(update_fields=['provisional_dwca_validation'])
        waiting = json.loads(_refinement_waiting_payload(record))

        refresh_refinement_report(self.dataset, waiting)
        self.dataset.refresh_from_db()
        saved = self.dataset.provisional_dwca_validation
        self.assertEqual(saved['status'], 'QUEUED')
        saved['queued_since'] = (timezone.now() - timedelta(minutes=45)).isoformat()
        self.dataset.save(update_fields=['provisional_dwca_validation'])

        refresh_refinement_report(self.dataset, waiting)

        discord.assert_called_once()
        self.assertIn("provisional DwC-A QUEUED for 45 minutes", discord.call_args.args[0])


class GbifPauseApiTests(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(username='api-user', password='test')
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.task = Task.objects.create(name='Data transformation', text='Transform', order=1)
        self.dataset = Dataset.objects.create(user=self.user, title='Working')
        self.agent = Agent.objects.create(dataset=self.dataset, task=self.task)
        Message.objects.create(agent=self.agent, openai_obj={'role': 'assistant', 'content': 'Anything else?'})

    def test_status_endpoint_is_public(self):
        gbif_pause.start('Validator stuck.')

        response = APIClient().get('/api/gbif-status/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.data['paused'], response.data['message']), (True, 'Validator stuck.'))

    def test_new_work_is_refused_while_paused(self):
        gbif_pause.start()

        created = self.client.post('/api/datasets/', {'title': 'New'}, format='json')
        reply = self.client.post('/api/messages/', {
            'agent': self.agent.id, 'openai_obj': {'role': 'user', 'content': 'Go on'},
        }, format='json')
        upload = self.client.post('/api/user-files/', {'dataset': self.dataset.id}, format='multipart')

        for response in (created, reply, upload):
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.data['detail'], gbif_pause.DEFAULT_NOTICE)
        self.assertEqual(Dataset.objects.count(), 1)
        self.assertEqual(self.agent.message_set.count(), 1)

    def test_conversion_choices_can_be_saved_but_work_is_not_queued(self):
        dataset = Dataset.objects.create(user=self.user, workflow_type=Dataset.WorkflowType.DWCA_CONVERSION)
        DwcConversion.objects.create(dataset=dataset, status='review', plan={'id': 'plan-1', 'columns': [], 'issues': [], 'tables': []})
        gbif_pause.start()

        inspect = self.client.post(f'/api/datasets/{dataset.id}/conversion/', {'action': 'inspect'}, format='json')
        save = self.client.post(f'/api/datasets/{dataset.id}/conversion/', {
            'action': 'save', 'plan_id': 'plan-1', 'changes': {},
        }, format='json')

        self.assertEqual(inspect.status_code, 503)
        self.assertNotEqual(save.status_code, 503)
        self.assertFalse(DwcConversionJob.objects.exists())

    def test_dataset_list_shows_working_datasets_as_paused(self):
        Message.objects.create(agent=self.agent, openai_obj={'role': 'user', 'content': 'Continue'})
        gbif_pause.start()

        response = self.client.get('/api/my-datasets/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data[0]['status'], 'paused')
