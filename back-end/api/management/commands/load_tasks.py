from django.core.management.base import BaseCommand
from django.core.management import call_command
from api.models import Task


class Command(BaseCommand):
    help = 'Load tasks from fixtures/tasks.yaml (removes existing tasks first)'

    def handle(self, *args, **options):
        self.stdout.write('Removing existing tasks...')
        
        # Remove all existing tasks
        deleted_count, _ = Task.objects.all().delete()
        self.stdout.write(f'Removed {deleted_count} existing tasks')
        
        self.stdout.write('Loading tasks from fixtures...')
        
        try:
            call_command('loaddata', 'tasks.yaml', verbosity=1)
            loaded_count = Task.objects.count()
            self.stdout.write(
                self.style.SUCCESS(f'Successfully loaded {loaded_count} tasks from fixtures/tasks.yaml')
            )
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f'Failed to load tasks: {e}')
            ) 