from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth import get_user_model
from allauth.account.signals import user_signed_up
from allauth.socialaccount.signals import pre_social_login
from allauth.socialaccount.models import SocialAccount

User = get_user_model()


@receiver(user_signed_up)
def handle_user_signed_up(sender, request, user, **kwargs):
    """Handle user signup through social authentication"""
    if user.socialaccount_set.filter(provider='orcid').exists():
        social_account = user.socialaccount_set.get(provider='orcid')
        extra_data = social_account.extra_data
        
        # Update user with ORCID information
        if 'orcid-identifier' in extra_data:
            user.orcid_id = extra_data['orcid-identifier'].get('path', '')
        
        # Extract name information
        if 'person' in extra_data:
            person = extra_data['person']
            if 'name' in person:
                name = person['name']
                if 'given-names' in name:
                    user.first_name = name['given-names'].get('value', '')
                if 'family-name' in name:
                    user.last_name = name['family-name'].get('value', '')
        
        # Extract employment information
        if 'activities-summary' in extra_data:
            activities = extra_data['activities-summary']
            if 'employments' in activities and 'employment-summary' in activities['employments']:
                employments = activities['employments']['employment-summary']
                if employments:
                    latest_employment = employments[0]  # Most recent employment
                    if 'organization' in latest_employment:
                        org = latest_employment['organization']
                        if 'name' in org:
                            user.institution = org['name']
                        if 'address' in org and 'city' in org['address']:
                            user.country = org['address'].get('country', '')
        
        user.save()


@receiver(pre_social_login)
def handle_pre_social_login(sender, request, sociallogin, **kwargs):
    """Handle pre-social login to update ORCID tokens"""
    if sociallogin.account.provider == 'orcid':
        user = sociallogin.user
        social_account = sociallogin.account
        
        # Update ORCID tokens
        user.orcid_access_token = social_account.token
        user.orcid_refresh_token = social_account.token_secret
        
        # Update ORCID ID if not already set
        if not user.orcid_id and 'orcid-identifier' in social_account.extra_data:
            user.orcid_id = social_account.extra_data['orcid-identifier'].get('path', '')
        
        user.save() 