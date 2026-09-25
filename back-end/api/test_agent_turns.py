from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from api.agent_turns import process_next_agent_turn, queue_agent_turn
from api.models import Agent, AgentTurnJob, CustomUser, Dataset, Message, Task


class AgentTurnQueueTests(TestCase):
    def setUp(self):
        # TestCase keeps one transaction open across each test; the production
        # worker closes idle connections between independent transactions.
        close_connections = patch('api.agent_turns.close_old_connections')
        close_connections.start()
        self.addCleanup(close_connections.stop)
        self.user = CustomUser.objects.create_user(username='turn-user', password='test')
        self.dataset = Dataset.objects.create(user=self.user, title='Queued workflow')
        self.task = Task.objects.create(name='Data transformation', text='Transform', order=1)
        self.agent = Agent.objects.create(dataset=self.dataset, task=self.task)
        self.message = Message.objects.create(agent=self.agent, openai_obj={
            'role': 'user', 'content': 'Continue',
        })

    def test_refresh_queues_once_without_running_model_in_http_request(self):
        self.client.force_login(self.user)
        with patch.object(Agent, 'next_message') as next_message:
            first = self.client.get(f'/api/datasets/{self.dataset.id}/refresh/')
            second = self.client.get(f'/api/datasets/{self.dataset.id}/refresh/')

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(AgentTurnJob.objects.count(), 1)
        self.assertEqual(AgentTurnJob.objects.get().requested_after_message_id, self.message.id)
        next_message.assert_not_called()

    def test_worker_runs_claimed_turn_and_clears_job(self):
        queue_agent_turn(self.agent)

        with patch.object(Agent, 'next_message') as next_message:
            self.assertTrue(process_next_agent_turn())

        next_message.assert_called_once()
        self.assertFalse(AgentTurnJob.objects.exists())

    def test_stale_claim_recovers_busy_flag(self):
        job = queue_agent_turn(self.agent)
        job.claimed_at = timezone.now() - timedelta(hours=2)
        job.save(update_fields=['claimed_at'])
        Agent.objects.filter(pk=self.agent.pk).update(busy_thinking=True)

        with patch.object(Agent, 'next_message') as next_message:
            self.assertTrue(process_next_agent_turn())

        next_message.assert_called_once()
        self.assertFalse(Agent.objects.get(pk=self.agent.pk).busy_thinking)

    def test_superseded_turn_is_not_replayed(self):
        queue_agent_turn(self.agent)
        Message.objects.create(agent=self.agent, openai_obj={'role': 'user', 'content': 'Changed'})

        with patch.object(Agent, 'next_message') as next_message:
            self.assertTrue(process_next_agent_turn())

        next_message.assert_not_called()
        self.assertFalse(AgentTurnJob.objects.exists())
