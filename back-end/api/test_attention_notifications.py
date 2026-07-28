from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient
from unittest.mock import patch

from api.attention_notifications import (
    arm_notification,
    dispatch_due_notifications,
    mark_notification_ready_if_needed,
)
from api.models import (
    Agent,
    CustomUser,
    Dataset,
    DatasetAttentionNotification,
    Message,
    Task,
)


class AttentionNotificationApiTests(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(
            username='owner@example.org',
            email='owner@example.org',
            password='password',
        )
        self.dataset = Dataset.objects.create(user=self.user, title='Long-running dataset')
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = reverse('dataset-attention-notification', args=[self.dataset.id])

    def test_owner_can_arm_and_cancel_an_email_notification(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'idle')
        self.assertEqual(response.data['suggested_email'], 'owner@example.org')

        response = self.client.post(self.url, {'email': 'notify@example.org'}, format='json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['status'], DatasetAttentionNotification.Status.PENDING)
        self.assertEqual(response.data['email'], 'notify@example.org')

        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], DatasetAttentionNotification.Status.CANCELLED)

    def test_orcid_placeholder_is_not_suggested(self):
        self.user.email = '0000-0000@orcid.org'
        self.user.save(update_fields=['email'])

        response = self.client.get(self.url)

        self.assertEqual(response.data['suggested_email'], '')

    def test_other_users_cannot_manage_dataset_notification(self):
        other = CustomUser.objects.create_user(
            username='other@example.org',
            email='other@example.org',
            password='password',
        )
        self.client.force_authenticate(other)

        response = self.client.post(self.url, {'email': 'other@example.org'}, format='json')

        self.assertEqual(response.status_code, 404)
        self.assertFalse(DatasetAttentionNotification.objects.exists())

    def test_invalid_email_is_rejected(self):
        response = self.client.post(self.url, {'email': 'not an email'}, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertIn('email', response.data)


@override_settings(
    ATTENTION_EMAIL_ENABLED=True,
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    DEFAULT_FROM_EMAIL='ChatIPT <chatipt@example.org>',
    FRONTEND_URL='https://chatipt.example.org',
)
class AttentionNotificationDispatchTests(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(
            username='owner@example.org',
            email='owner@example.org',
            password='password',
        )
        self.dataset = Dataset.objects.create(user=self.user, title='Test dataset')
        self.task = Task.objects.create(name='Review', text='Review this dataset', order=1)
        self.agent = Agent.objects.create(dataset=self.dataset, task=self.task)

    def test_assistant_question_queues_and_sends_one_email(self):
        notification = arm_notification(self.dataset, 'notify@example.org')
        self.assertEqual(notification.status, DatasetAttentionNotification.Status.PENDING)
        Message.objects.create(
            agent=self.agent,
            openai_obj={'role': Message.Role.ASSISTANT, 'content': 'Which sheet should I use?'},
        )

        self.assertTrue(mark_notification_ready_if_needed(self.dataset))
        result = dispatch_due_notifications()

        notification.refresh_from_db()
        self.assertEqual(result, {'sent': 1, 'failed': 0, 'disabled': False})
        self.assertEqual(notification.status, DatasetAttentionNotification.Status.SENT)
        self.assertEqual(notification.attention_kind, DatasetAttentionNotification.AttentionKind.NEEDS_INPUT)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('needs your attention', mail.outbox[0].subject)
        self.assertIn(self.dataset.title, mail.outbox[0].subject)
        self.assertIn(f'Dataset: {self.dataset.title}', mail.outbox[0].body)
        self.assertIn(f'/dataset/{self.dataset.id}', mail.outbox[0].body)

        self.assertEqual(dispatch_due_notifications()['sent'], 0)
        self.assertEqual(len(mail.outbox), 1)

    def test_notification_is_queued_when_agent_finishes_thinking(self):
        notification = arm_notification(self.dataset, 'notify@example.org')
        self.agent.busy_thinking = True
        self.agent.save(update_fields=['busy_thinking'])
        with self.captureOnCommitCallbacks(execute=True):
            Message.objects.create(
                agent=self.agent,
                openai_obj={'role': Message.Role.ASSISTANT, 'content': 'Please review this choice.'},
            )
        notification.refresh_from_db()
        self.assertEqual(notification.status, DatasetAttentionNotification.Status.PENDING)

        self.agent.busy_thinking = False
        with self.captureOnCommitCallbacks(execute=True):
            self.agent.save(update_fields=['busy_thinking'])

        notification.refresh_from_db()
        self.assertEqual(notification.status, DatasetAttentionNotification.Status.READY)
        self.assertEqual(
            notification.attention_kind,
            DatasetAttentionNotification.AttentionKind.NEEDS_INPUT,
        )

    def test_ready_package_is_an_attention_event(self):
        arm_notification(self.dataset, 'notify@example.org')
        self.dataset.dwc_dp_url = 'https://example.org/package.tar.gz'
        self.dataset.dwca_url = 'https://example.org/archive.zip'
        self.dataset.dwc_dp_validation = {'valid': True}
        self.dataset.save()

        self.assertTrue(mark_notification_ready_if_needed(self.dataset))
        dispatch_due_notifications()

        notification = self.dataset.attention_notification
        notification.refresh_from_db()
        self.assertEqual(notification.attention_kind, DatasetAttentionNotification.AttentionKind.READY)
        self.assertEqual(notification.status, DatasetAttentionNotification.Status.SENT)
        self.assertIn('package is ready', mail.outbox[0].subject)

    def test_delivery_failure_is_queued_for_retry(self):
        notification = arm_notification(self.dataset, 'notify@example.org')
        Message.objects.create(
            agent=self.agent,
            openai_obj={'role': Message.Role.ASSISTANT, 'content': 'I need an answer.'},
        )
        mark_notification_ready_if_needed(self.dataset)

        with patch('api.attention_notifications.send_mail', side_effect=RuntimeError('SMTP unavailable')):
            result = dispatch_due_notifications()

        notification.refresh_from_db()
        self.assertEqual(result, {'sent': 0, 'failed': 1, 'disabled': False})
        self.assertEqual(notification.status, DatasetAttentionNotification.Status.READY)
        self.assertEqual(notification.attempt_count, 1)
        self.assertIsNotNone(notification.next_attempt_at)
        self.assertIn('SMTP unavailable', notification.last_error)
