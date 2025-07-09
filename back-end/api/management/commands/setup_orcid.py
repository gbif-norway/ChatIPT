from django.core.management.base import BaseCommand
from django.contrib.sites.models import Site
from allauth.socialaccount.models import SocialApp
from django.conf import settings


class Command(BaseCommand):
    help = 'Set up ORCID OAuth provider and site configuration'

    def handle(self, *args, **options):
        # Create or update the site
        site, created = Site.objects.get_or_create(
            id=settings.SITE_ID,
            defaults={
                'domain': 'localhost:8000',
                'name': 'ChatIPT'
            }
        )
        
        if created:
            self.stdout.write(
                self.style.SUCCESS(f'Created site: {site.name} ({site.domain})')
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(f'Site already exists: {site.name} ({site.domain})')
            )

        # Create ORCID social app
        orcid_app, created = SocialApp.objects.get_or_create(
            provider='orcid',
            defaults={
                'name': 'ORCID',
                'client_id': settings.SOCIALACCOUNT_PROVIDERS['orcid']['APP']['client_id'],
                'secret': settings.SOCIALACCOUNT_PROVIDERS['orcid']['APP']['secret'],
            }
        )
        
        if created:
            self.stdout.write(
                self.style.SUCCESS('Created ORCID social app')
            )
        else:
            # Update existing app with new credentials
            orcid_app.client_id = settings.SOCIALACCOUNT_PROVIDERS['orcid']['APP']['client_id']
            orcid_app.secret = settings.SOCIALACCOUNT_PROVIDERS['orcid']['APP']['secret']
            orcid_app.save()
            self.stdout.write(
                self.style.SUCCESS('Updated ORCID social app')
            )

        # Add site to the social app
        orcid_app.sites.add(site)
        
        self.stdout.write(
            self.style.SUCCESS('Successfully set up ORCID OAuth provider')
        ) 