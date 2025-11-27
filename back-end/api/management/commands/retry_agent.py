"""
Management command to retry an agent's last tool call by deleting recent messages.

Usage:
    python manage.py retry_agent <dataset_id> [--delete-count N] [--dry-run]

Examples:
    # See what would be deleted for dataset 123
    python manage.py retry_agent 123 --dry-run

    # Delete the last 2 messages (tool result + assistant response) and retry
    python manage.py retry_agent 123 --delete-count 2

    # Just delete messages without triggering refresh
    python manage.py retry_agent 123 --delete-count 2 --no-refresh
"""

from django.core.management.base import BaseCommand
from api.models import Dataset, Agent, Message


class Command(BaseCommand):
    help = 'Delete recent messages from an agent to retry a failed tool call'

    def add_arguments(self, parser):
        parser.add_argument('dataset_id', type=int, help='Dataset ID to retry')
        parser.add_argument(
            '--delete-count',
            type=int,
            default=2,
            help='Number of messages to delete from the end (default: 2)'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be deleted without actually deleting'
        )
        parser.add_argument(
            '--no-refresh',
            action='store_true',
            help='Do not trigger agent refresh after deleting messages'
        )

    def handle(self, *args, **options):
        dataset_id = options['dataset_id']
        delete_count = options['delete_count']
        dry_run = options['dry_run']
        no_refresh = options['no_refresh']

        try:
            dataset = Dataset.objects.get(id=dataset_id)
        except Dataset.DoesNotExist:
            self.stderr.write(self.style.ERROR(f'Dataset {dataset_id} not found'))
            return

        # Get the current/last agent
        agent = dataset.agent_set.order_by('-created_at').first()
        if not agent:
            self.stderr.write(self.style.ERROR(f'No agent found for dataset {dataset_id}'))
            return

        self.stdout.write(f'Dataset: {dataset_id} - {dataset.title or "(no title)"}')
        self.stdout.write(f'Agent ID: {agent.id}, Task: {agent.task.name}')
        self.stdout.write(f'Agent completed_at: {agent.completed_at}')
        self.stdout.write(f'Agent busy_thinking: {agent.busy_thinking}')
        self.stdout.write('')

        # Get recent messages
        messages = list(agent.message_set.order_by('-created_at')[:10])
        
        self.stdout.write('Recent messages (newest first):')
        for i, msg in enumerate(messages):
            role = msg.openai_obj.get('role', 'unknown')
            content = msg.openai_obj.get('content', '')
            tool_calls = msg.openai_obj.get('tool_calls', [])
            tool_call_id = msg.openai_obj.get('tool_call_id', '')
            
            content_preview = (content[:100] + '...') if content and len(content) > 100 else content
            
            marker = ' [WILL DELETE]' if i < delete_count else ''
            
            if tool_calls:
                tool_names = [tc.get('function', {}).get('name', 'unknown') for tc in tool_calls]
                self.stdout.write(f'  {i+1}. [{role}] tool_calls: {tool_names}{marker}')
            elif tool_call_id:
                self.stdout.write(f'  {i+1}. [{role}] tool_result for {tool_call_id}: {content_preview}{marker}')
            else:
                self.stdout.write(f'  {i+1}. [{role}] {content_preview}{marker}')
        
        self.stdout.write('')

        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN - No changes made'))
            self.stdout.write(f'Would delete {delete_count} message(s)')
            return

        # Delete messages
        messages_to_delete = messages[:delete_count]
        for msg in messages_to_delete:
            self.stdout.write(f'Deleting message {msg.id} (role: {msg.openai_obj.get("role")})')
            msg.delete()

        self.stdout.write(self.style.SUCCESS(f'Deleted {len(messages_to_delete)} message(s)'))

        # Clear busy_thinking if it was stuck
        if agent.busy_thinking:
            agent.busy_thinking = False
            agent.save(update_fields=['busy_thinking'])
            self.stdout.write('Cleared busy_thinking flag')

        # Show what the last message is now
        new_last_message = agent.message_set.order_by('-created_at').first()
        if new_last_message:
            role = new_last_message.openai_obj.get('role', 'unknown')
            self.stdout.write(f'New last message role: {role}')

        if not no_refresh:
            self.stdout.write('Triggering agent.next_message()...')
            try:
                result = agent.next_message()
                if result:
                    self.stdout.write(self.style.SUCCESS(f'Agent responded with {len(result)} new message(s)'))
                else:
                    self.stdout.write('Agent returned None (may need a user message to continue)')
            except Exception as e:
                self.stderr.write(self.style.ERROR(f'Error during next_message: {e}'))
        else:
            self.stdout.write('Skipping refresh (--no-refresh)')

