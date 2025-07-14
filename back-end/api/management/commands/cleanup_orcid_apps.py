from django.core.management.base import BaseCommand
from allauth.socialaccount.models import SocialApp


class Command(BaseCommand):
    help = 'Clean up duplicate ORCID social apps'

    def handle(self, *args, **options):
        # Get all ORCID apps
        orcid_apps = SocialApp.objects.filter(provider='orcid')
        
        if orcid_apps.count() > 1:
            self.stdout.write(f'Found {orcid_apps.count()} ORCID apps. Cleaning up...')
            
            # Keep the first one and delete the rest
            first_app = orcid_apps.first()
            apps_to_delete = orcid_apps.exclude(id=first_app.id)
            
            for app in apps_to_delete:
                self.stdout.write(f'Deleting duplicate ORCID app: {app.name} (ID: {app.id})')
                app.delete()
            
            self.stdout.write(
                self.style.SUCCESS(f'Successfully cleaned up ORCID apps. Kept: {first_app.name} (ID: {first_app.id})')
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(f'Found {orcid_apps.count()} ORCID app(s). No cleanup needed.')
            ) 