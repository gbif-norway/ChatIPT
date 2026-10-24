from django.core.management.base import BaseCommand

from api import gbif_pause


class Command(BaseCommand):
    help = "Pause or resume all ChatIPT processing while part of GBIF's API is down."

    def add_arguments(self, parser):
        parser.add_argument('state', choices=['on', 'off', 'status'])
        parser.add_argument('--message', default='', help='Notice shown to users instead of the default one.')
        parser.add_argument('--by', default='manage.py', help='Who switched it, for the outage history.')

    def handle(self, *args, **options):
        if options['state'] == 'on':
            outage, created = gbif_pause.start(options['message'], by=options['by'])
            self.stdout.write(
                f"{'Paused' if created else 'Already paused'} since {outage.started_at:%Y-%m-%d %H:%M %Z}. "
                f"Users see: {gbif_pause.notice(outage)}"
            )
        elif options['state'] == 'off':
            outage = gbif_pause.end(by=options['by'])
            self.stdout.write(f'Resumed; paused since {outage.started_at:%Y-%m-%d %H:%M %Z}.' if outage else 'Not paused.')
        else:
            outage = gbif_pause.current()
            self.stdout.write(
                f'Paused since {outage.started_at:%Y-%m-%d %H:%M %Z}. Users see: {gbif_pause.notice(outage)}'
                if outage else 'Not paused.'
            )
