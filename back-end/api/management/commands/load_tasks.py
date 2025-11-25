from django.core.management.base import BaseCommand
from api.models import Task
import os
import yaml


class Command(BaseCommand):
    help = 'Upsert tasks from fixtures/tasks.yaml without deleting existing records (preserves FKs)'

    def handle(self, *args, **options):
        # Resolve path to api/fixtures/tasks.yaml relative to this file
        fixtures_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            'fixtures',
            'tasks.yaml',
        )

        if not os.path.exists(fixtures_path):
            self.stdout.write(self.style.ERROR(f'Fixture not found: {fixtures_path}'))
            return

        self.stdout.write(f'Upserting tasks from {fixtures_path} ...')

        with open(fixtures_path, 'r', encoding='utf-8') as f:
            try:
                data = yaml.safe_load(f) or []
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'Failed to parse YAML: {e}'))
                return

        if not isinstance(data, list):
            self.stdout.write(self.style.ERROR('Expected a list of objects in tasks.yaml'))
            return

        upserted = 0
        created = 0

        for order, obj in enumerate(data, start=1):
            if not isinstance(obj, dict):
                continue
            if obj.get('model') != 'api.task':
                continue
            fields = obj.get('fields') or {}
            name = fields.get('name')
            text = fields.get('text', '')
            if not name:
                continue

            task, was_created = Task.objects.update_or_create(
                name=name,
                defaults={'text': text, 'order': order},
            )
            upserted += 1
            if was_created:
                created += 1

        self.stdout.write(self.style.SUCCESS(
            f'Upserted {upserted} tasks ({created} created, {upserted - created} updated).'))

        # Optional: Report on tasks present in DB but not in the fixture (we do NOT delete them)
        fixture_names = {
            (obj.get('fields') or {}).get('name')
            for obj in data
            if isinstance(obj, dict) and obj.get('model') == 'api.task'
        }
        missing = Task.objects.exclude(name__in=fixture_names).count()
        if missing:
            self.stdout.write(
                self.style.WARNING(
                    f'{missing} existing task(s) not present in fixture were left untouched.'
                )
            )