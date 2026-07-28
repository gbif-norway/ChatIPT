import time

from django.core.management.base import BaseCommand

from api.attention_notifications import dispatch_due_notifications


class Command(BaseCommand):
    help = 'Send queued dataset attention emails.'

    def add_arguments(self, parser):
        parser.add_argument('--watch', action='store_true', help='Keep polling for notifications.')
        parser.add_argument('--interval', type=float, default=10, help='Polling interval in seconds.')
        parser.add_argument('--limit', type=int, default=20, help='Maximum emails per polling pass.')

    def handle(self, *args, **options):
        warned_disabled = False
        while True:
            try:
                result = dispatch_due_notifications(limit=options['limit'])
            except Exception as exc:
                if not options['watch']:
                    raise
                self.stderr.write(f'Attention email worker pass failed: {exc}')
                time.sleep(max(options['interval'], 1))
                continue
            if result.get('disabled') and not warned_disabled:
                self.stdout.write('Attention email worker is idle because Gmail credentials are not configured.')
                warned_disabled = True
            elif result['sent'] or result['failed']:
                self.stdout.write(
                    f"Attention email pass: {result['sent']} sent, {result['failed']} failed."
                )

            if not options['watch']:
                break
            time.sleep(max(options['interval'], 1))
