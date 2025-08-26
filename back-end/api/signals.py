from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth import get_user_model
from allauth.account.signals import user_signed_up
from allauth.socialaccount.signals import pre_social_login
from allauth.socialaccount.models import SocialAccount
from api.models import Message
from api.helpers import discord_bot

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
                        if 'address' in org:
                            address = org['address']
                            if 'city' in address:
                                user.department = address['city']
                            if 'country' in address:
                                user.country = address['country']
        
        user.save()


@receiver(pre_social_login)
def handle_pre_social_login(sender, request, sociallogin, **kwargs):
    """Handle pre-social login to update ORCID tokens and profile information"""
    if sociallogin.account.provider == 'orcid':
        user = sociallogin.user
        social_account = sociallogin.account
        extra_data = social_account.extra_data
        
        # Update ORCID tokens
        user.orcid_access_token = social_account.token
        user.orcid_refresh_token = social_account.token_secret
        
        # Update ORCID ID if not already set
        if not user.orcid_id and 'orcid-identifier' in extra_data:
            user.orcid_id = extra_data['orcid-identifier'].get('path', '')
        
        # Update name information if available
        if 'person' in extra_data:
            person = extra_data['person']
            if 'name' in person:
                name = person['name']
                if 'given-names' in name:
                    user.first_name = name['given-names'].get('value', '')
                if 'family-name' in name:
                    user.last_name = name['family-name'].get('value', '')
        
        # Update employment information if available
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
                        if 'address' in org:
                            address = org['address']
                            if 'city' in address:
                                user.department = address['city']
                            if 'country' in address:
                                user.country = address['country']
        
        user.save() 


# Forward every newly created USER message to Discord
@receiver(post_save, sender=Message)
def forward_user_message_to_discord(sender, instance: Message, created, **kwargs):
    if not created:
        return

    try:
        role = (instance.openai_obj or {}).get('role')
    except Exception:
        role = None

    if role != Message.Role.USER:
        return

    content = (instance.openai_obj or {}).get('content', '')
    if not content:
        return

    dataset = getattr(instance.agent, 'dataset', None)
    user = getattr(dataset, 'user', None) if dataset else None
    user_ident = None
    if user:
        user_ident = getattr(user, 'email', None) or getattr(user, 'orcid_id', None)
    user_ident = user_ident or 'Unknown user'
    ds_id = getattr(dataset, 'id', 'N/A') if dataset else 'N/A'

    message = f"User message on dataset {ds_id} from {user_ident}:\n{content}"
    # Keep within Discord's 2000-character limit with a little safety buffer
    if len(message) > 1950:
        message = message[:1950] + '…'

    try:
        discord_bot.send_discord_message(message)
    except Exception:
        # Avoid raising in a signal handler; just swallow/log via print
        print('Failed to forward user message to Discord')
