import time

from django.core.management.base import BaseCommand

from api.agent_turns import process_next_agent_turn


class Command(BaseCommand):
    help = 'Run queued agent turns outside web requests.'

    def add_arguments(self, parser):
        parser.add_argument('--watch', action='store_true', help='Keep polling for turns.')
        parser.add_argument('--interval', type=float, default=2, help='Idle polling interval in seconds.')

    def handle(self, *args, **options):
        while True:
            try:
                worked = process_next_agent_turn()
            except Exception as exc:
                if not options['watch']:
                    raise
                self.stderr.write(f'Agent turn worker pass failed: {exc}')
                worked = False
            if not options['watch']:
                break
            if not worked:
                time.sleep(max(options['interval'], 1))
