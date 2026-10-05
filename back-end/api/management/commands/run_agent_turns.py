import time

from django.conf import settings
from django.core.management.base import BaseCommand

from api.agent_turns import process_next_agent_turn, reconcile_recent_work
from api.conversion_jobs import process_next_conversion


class Command(BaseCommand):
    help = 'Run queued agent turns outside web requests.'

    def add_arguments(self, parser):
        parser.add_argument('--watch', action='store_true', help='Keep polling for turns.')
        parser.add_argument('--interval', type=float, default=2, help='Idle polling interval in seconds.')
        parser.add_argument('--reconcile', action='store_true', help='Recover recently active work without a queued turn.')

    def handle(self, *args, **options):
        if options['watch'] and settings.DEBUG:
            # These workers start beside runserver, so its autoreloader does not
            # restart them when conversion code changes in a local container.
            from django.utils.autoreload import run_with_reloader
            return run_with_reloader(self.run_loop, options)
        return self.run_loop(options)

    def run_loop(self, options):
        next_reconcile_at = 0
        while True:
            if options['reconcile'] and time.monotonic() >= next_reconcile_at:
                try:
                    reconcile_recent_work()
                except Exception as exc:
                    if not options['watch']:
                        raise
                    self.stderr.write(f'Agent work reconciliation failed: {exc}')
                next_reconcile_at = time.monotonic() + 300
            try:
                worked = process_next_conversion() or process_next_agent_turn()
            except Exception as exc:
                if not options['watch']:
                    raise
                self.stderr.write(f'Agent turn worker pass failed: {exc}')
                worked = False
            if not options['watch']:
                break
            if not worked:
                time.sleep(max(options['interval'], 1))
