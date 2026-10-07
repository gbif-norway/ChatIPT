from rest_framework import serializers, viewsets, status
from rest_framework.exceptions import ValidationError
from api.serializers import DatasetSerializer, DatasetListSerializer, OpenAIUsageSerializer, TablePageQuerySerializer, TableSerializer, MessageSerializer, AgentSerializer, TaskSerializer, TaxonDecisionSerializer, TaxonNameMatchSerializer, UserFileSerializer
from api.models import Dataset, Table, Message, Agent, Task, TaxonNameMatch, UserFile
from rest_framework.response import Response
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import IsAuthenticated, AllowAny, BasePermission
from rest_framework.throttling import UserRateThrottle
from django.shortcuts import get_object_or_404, redirect
from django.contrib.auth import get_user_model
from rest_framework.serializers import ModelSerializer
from django.conf import settings
from django.db import transaction
import requests
from pathlib import Path
from urllib.parse import urlencode
import logging
from math import isfinite
from api.attention_notifications import (
    arm_notification,
    cancel_notification,
    notification_payload,
)
from api.agent_turns import ensure_dataset_work
from api.helpers.publish import clean_person_name

logger = logging.getLogger(__name__)
PRIVATE_PROFILE_STATUS_CODES = {401, 403, 404}

User = get_user_model()


class AttentionNotificationRateThrottle(UserRateThrottle):
    rate = '10/hour'

    def allow_request(self, request, view):
        if request.method == 'GET':
            return True
        return super().allow_request(request, view)


class ConversionReviewRateThrottle(UserRateThrottle):
    scope = 'conversion_review'
    rate = '10/hour'

    def allow_request(self, request, view):
        if request.method == 'GET' or request.data.get('action') != 'review':
            return True
        return super().allow_request(request, view)


class ConversionChatRateThrottle(UserRateThrottle):
    scope = 'conversion_chat'
    rate = '60/hour'

    def allow_request(self, request, view):
        if request.method == 'GET' or request.data.get('action') != 'chat' or 'confirm' in request.data:
            return True
        return super().allow_request(request, view)


class IsAuthenticatedOrSuperuser(BasePermission):
    """
    Custom permission that allows authenticated users to access their own data,
    but allows superusers to access all data.
    """
    def has_permission(self, request, view):
        return request.user and request.user.is_authenticated
    
    def has_object_permission(self, request, view, obj):
        # Superusers can access everything
        if request.user.is_superuser:
            return True
        
        # Regular users can only access their own data
        if hasattr(obj, 'user'):
            return obj.user == request.user
        elif hasattr(obj, 'dataset') and hasattr(obj.dataset, 'user'):
            return obj.dataset.user == request.user
        elif hasattr(obj, 'agent') and hasattr(obj.agent, 'dataset') and hasattr(obj.agent.dataset, 'user'):
            return obj.agent.dataset.user == request.user
        
        return False


class UserSerializer(ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'email', 'first_name', 'last_name', 'orcid_id', 'institution', 'department', 'country', 'is_superuser']
        read_only_fields = ['id', 'email', 'orcid_id', 'is_superuser']


@api_view(['GET'])
@permission_classes([AllowAny])
def auth_status(request):
    """Check authentication status and return user info if authenticated"""
    logger.info(f"Auth status check - User authenticated: {request.user.is_authenticated}")
    logger.info(f"User ID: {request.user.id if request.user.is_authenticated else 'None'}")
    logger.info(f"Session ID: {request.session.session_key}")
    logger.info(f"Session data: {dict(request.session)}")
    
    if request.user.is_authenticated:
        serializer = UserSerializer(request.user)
        return Response({
            'authenticated': True,
            'user': serializer.data
        })
    else:
        return Response({
            'authenticated': False,
            'user': None
        })


@api_view(['GET'])
@permission_classes([AllowAny])
def csrf_token(request):
    """Get CSRF token for frontend"""
    from django.middleware.csrf import get_token
    return Response({'csrfToken': get_token(request)})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def user_profile(request):
    """Get current user's profile information"""
    serializer = UserSerializer(request.user)
    return Response(serializer.data)


def get_orcid_url(endpoint: str) -> str:
    """Return the correct ORCID URL (sandbox or production) for a given endpoint ('authorize', 'token', 'userinfo', 'public_api', 'public_api_record')."""
    sandbox = {
        'authorize': 'https://sandbox.orcid.org/oauth/authorize',
        'token': 'https://sandbox.orcid.org/oauth/token',
        'userinfo': 'https://sandbox.orcid.org/oauth/userinfo',
        'public_api': 'https://pub.sandbox.orcid.org/v3.0',
        'public_api_record': 'https://pub.sandbox.orcid.org/v3.0',
    }
    production = {
        'authorize': 'https://orcid.org/oauth/authorize',
        'token': 'https://orcid.org/oauth/token',
        'userinfo': 'https://orcid.org/oauth/userinfo',
        'public_api': 'https://pub.orcid.org/v3.0',
        'public_api_record': 'https://pub.orcid.org/v3.0',
    }
    # Use the environment variable set in settings to decide which ORCID host to use
    if settings.ORCID_ENV == "sandbox":
        return sandbox[endpoint]
    return production[endpoint]


def get_orcid_scope_string() -> str:
    """Return a space-delimited scope string configured for ORCID."""
    provider_settings = settings.SOCIALACCOUNT_PROVIDERS.get('orcid', {})
    scopes = provider_settings.get('SCOPE', [])
    if isinstance(scopes, (list, tuple, set)):
        return ' '.join(scopes)
    return scopes or 'openid /authenticate'


@api_view(['GET'])
@permission_classes([AllowAny])
def orcid_login(request):
    """Redirect to ORCID OAuth2 authorization endpoint"""
    client_id = settings.SOCIALACCOUNT_PROVIDERS['orcid']['APP']['client_id']
    # Callback should go to backend, not frontend
    base_url = request.build_absolute_uri('/').rstrip('/')
    redirect_uri = f"{base_url}/api/auth/orcid/callback/"
    
    logger.info(f"ORCID login - Base URL: {base_url}")
    logger.info(f"ORCID login - Redirect URI: {redirect_uri}")
    logger.info(f"ORCID login - Client ID: {client_id}")
    
    # ORCID OAuth2 authorization URL
    auth_url = get_orcid_url('authorize')
    scope = get_orcid_scope_string()
    
    # OAuth2 parameters - using public API scopes only
    params = {
        'client_id': client_id,
        'response_type': 'code',
        'scope': scope,
        'redirect_uri': redirect_uri,
    }
    
    auth_url_with_params = f"{auth_url}?{urlencode(params)}"
    logger.info(f"Redirecting to ORCID: {auth_url_with_params}")
    
    # Redirect to ORCID
    return redirect(auth_url_with_params)


@api_view(['GET'])
@permission_classes([AllowAny])
def orcid_callback(request):
    """Handle ORCID OAuth2 callback"""
    logger.info(f"ORCID callback received with params: {dict(request.GET)}")
    
    code = request.GET.get('code')
    error = request.GET.get('error')
    
    if error:
        logger.error(f"ORCID returned error: {error}")
        return redirect(f"{settings.FRONTEND_URL}?error={error}")
    
    if not code:
        logger.error("No authorization code received from ORCID")
        return redirect(f"{settings.FRONTEND_URL}?error=no_code")
    
    try:
        # Exchange code for access token
        token_url = get_orcid_url('token')
        client_id = settings.SOCIALACCOUNT_PROVIDERS['orcid']['APP']['client_id']
        client_secret = settings.SOCIALACCOUNT_PROVIDERS['orcid']['APP']['secret']
        base_url = request.build_absolute_uri('/').rstrip('/')
        redirect_uri = f"{base_url}/api/auth/orcid/callback/"
        
        token_data = {
            'grant_type': 'authorization_code',
            'client_id': client_id,
            'client_secret': client_secret,
            'code': code,
            'redirect_uri': redirect_uri,
        }
        
        response = requests.post(token_url, data=token_data)
        response.raise_for_status()
        token_info = response.json()
        
        logger.debug(f"Token response status: {response.status_code}")
        logger.debug(f"Token info keys: {list(token_info.keys())}")
        logger.debug(f"Access token present: {'access_token' in token_info}")
        
        # Get user info from ORCID
        access_token = token_info['access_token']
        
        # First get basic user info from userinfo endpoint
        user_info_url = get_orcid_url('userinfo')
        headers = {'Authorization': f'Bearer {access_token}'}
        
        user_response = requests.get(user_info_url, headers=headers)
        user_response.raise_for_status()
        user_info = user_response.json()
        
        # Get ORCID ID from userinfo
        orcid_id = user_info.get('sub')
        if not orcid_id:
            logger.error(f"ORCID userinfo response: {user_info}")
            return redirect(f"{settings.FRONTEND_URL}?error=no_orcid_id")
        
        # Get public profile info from ORCID public API
        public_data = {}
        public_headers = {'Accept': 'application/json'}
        try:
            public_url = f"{get_orcid_url('public_api')}/{orcid_id}"
            logger.debug(f"Fetching public profile data from: {public_url}")
            public_response = requests.get(public_url, headers=public_headers)
            logger.debug(f"Public API response status: {public_response.status_code}")
            
            if public_response.status_code in PRIVATE_PROFILE_STATUS_CODES:
                logger.warning(
                    f"ORCID record {orcid_id} does not expose public data (status {public_response.status_code})."
                )
                return redirect(f"{settings.FRONTEND_URL}?error=public_profile_required")
            
            if public_response.status_code != 200:
                logger.warning(f"Public API returned {public_response.status_code}, trying record endpoint")
                public_url = f"{get_orcid_url('public_api_record')}/{orcid_id}/record"
                logger.debug(f"Trying alternative endpoint: {public_url}")
                public_response = requests.get(public_url, headers=public_headers)
                logger.debug(f"Alternative endpoint response status: {public_response.status_code}")
                if public_response.status_code in PRIVATE_PROFILE_STATUS_CODES:
                    logger.warning(
                        f"ORCID record {orcid_id} record endpoint still private (status {public_response.status_code})."
                    )
                    return redirect(f"{settings.FRONTEND_URL}?error=public_profile_required")
            
            public_response.raise_for_status()
            public_data = public_response.json()
        except requests.RequestException as public_error:
            status_code = getattr(getattr(public_error, 'response', None), 'status_code', None)
            if status_code in PRIVATE_PROFILE_STATUS_CODES:
                logger.warning(
                    f"Unable to fetch ORCID public profile for {orcid_id} due to status {status_code}. "
                    "Account appears to be private."
                )
                return redirect(f"{settings.FRONTEND_URL}?error=public_profile_required")
            logger.warning(
                f"Unable to fetch ORCID public profile for {orcid_id}: {public_error}. "
                "Continuing with userinfo response only."
            )
        
        # Extract user information from public data
        email = None
        first_name = None
        last_name = None
        institution = None
        department = None
        country = None
        
        if 'person' in public_data:
            person = public_data['person']
            
            # Extract name information (available in public profile)
            if 'name' in person:
                name = person['name']
                if 'given-names' in name:
                    first_name = (name.get('given-names') or {}).get('value', '')
                if 'family-name' in name:
                    last_name = (name.get('family-name') or {}).get('value', '')
            
            # Extract employment information from public profile
            if 'employments' in person and 'employment-summary' in person['employments']:
                employments = person['employments']['employment-summary']
                if employments:
                    # Get the most recent employment
                    latest_employment = employments[0]
                    if 'employment-summary' in latest_employment:
                        employment = latest_employment['employment-summary']
                        if 'organization' in employment:
                            org = employment['organization']
                            if 'name' in org:
                                institution = org['name']
                            if 'address' in org:
                                address = org['address']
                                if 'city' in address:
                                    department = address['city']
                                if 'country' in address:
                                    country = address['country']
                    elif 'organization' in latest_employment:
                        # Alternative structure
                        org = latest_employment['organization']
                        if 'name' in org:
                            institution = org['name']
                        if 'address' in org:
                            address = org['address']
                            if 'city' in address:
                                department = address['city']
                            if 'country' in address:
                                country = address['country']
        
        # Try to get email from userinfo (may not be available with public API)
        if not email:
            email = user_info.get('email')
        
        # If no email available, create a placeholder email using ORCID ID
        if not email:
            email = f"{orcid_id}@orcid.org"
            logger.info(f"No email found in ORCID profile, using placeholder: {email}")
        
        logger.info(f"ORCID ID: {orcid_id}")
        logger.info(f"Email: {email}")
        logger.info(f"First Name: {first_name}")
        logger.info(f"Last Name: {last_name}")
        logger.info(f"Institution: {institution}")
        logger.info(f"Department: {department}")
        logger.info(f"Country: {country}")
        logger.debug(f"User info: {user_info}")
        logger.debug(f"Public data keys: {list(public_data.keys()) if public_data else 'No public data'}")
        
        # Debug employment data structure
        if 'person' in public_data and 'employments' in public_data['person']:
            logger.debug(f"Employment data structure: {public_data['person']['employments']}")
        
        user = User.objects.filter(email__iexact=email).first()
        created = user is None
        if created:
            user = User.objects.create(
                email=email,
                username=email,
                orcid_id=orcid_id,
                orcid_access_token=access_token,
                orcid_refresh_token=token_info.get('refresh_token', ''),
                first_name=clean_person_name(first_name) or '',
                last_name=clean_person_name(last_name) or '',
                institution=institution or '',
                department=department or '',
                country=country or '',
                is_active=True,
            )
        
        if not created:
            # Update existing user's ORCID info and profile data
            user.orcid_id = orcid_id
            user.orcid_access_token = access_token
            user.orcid_refresh_token = token_info.get('refresh_token', '')
            user.is_active = True  # Ensure user is active
            
            # Fill missing profile names from ORCID without replacing a name
            # the account holder has already supplied.
            if not clean_person_name(user.first_name) and clean_person_name(first_name):
                user.first_name = first_name
            if not clean_person_name(user.last_name) and clean_person_name(last_name):
                user.last_name = last_name
            if institution:
                user.institution = institution
            if department:
                user.department = department
            if country:
                user.country = country
            
            user.save()
        
        logger.info(f"User created/updated: {user.id}, Email: {user.email}, Is Active: {user.is_active}")
        
        # Log the user in
        from django.contrib.auth import login
        from django.contrib.auth.backends import ModelBackend
        login(request, user, backend='django.contrib.auth.backends.ModelBackend')
        
        logger.info(f"User logged in: {request.user.is_authenticated}, User ID: {request.user.id if request.user.is_authenticated else 'None'}")
        
        # Redirect back to frontend
        return redirect(settings.FRONTEND_URL)
        
    except Exception as e:
        logger.error(f"ORCID callback error: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return redirect(f"{settings.FRONTEND_URL}?error=callback_failed")


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def my_datasets(request):
    """Return list of datasets for dashboard with lightweight card data"""
    # Superusers can see all datasets, regular users only see their own
    if request.user.is_superuser:
        datasets = Dataset.objects.all().order_by('-created_at')
    else:
        datasets = Dataset.objects.filter(user=request.user).order_by('-created_at')
    
    serializer = DatasetListSerializer(datasets, many=True, context={'request': request})
    return Response(serializer.data)


class DatasetViewSet(viewsets.ModelViewSet):
    serializer_class = DatasetSerializer
    permission_classes = [IsAuthenticatedOrSuperuser]
    filterset_fields = ['created_at', 'orcid']

    def get_queryset(self):
        """Filter datasets to only show those belonging to the authenticated user, unless user is superuser"""
        logger.info(f"DatasetViewSet.get_queryset - User authenticated: {self.request.user.is_authenticated}")
        logger.info(f"User ID: {self.request.user.id if self.request.user.is_authenticated else 'None'}")
        logger.info(f"Is superuser: {self.request.user.is_superuser if self.request.user.is_authenticated else 'None'}")
        logger.info(f"Session ID: {self.request.session.session_key}")
        
        # Superusers can see all datasets
        if self.request.user.is_superuser:
            return Dataset.objects.all()
        
        # Regular users only see their own datasets
        return Dataset.objects.filter(user=self.request.user)

    def get_serializer_context(self):
        """Add request context to serializer"""
        context = super().get_serializer_context()
        context['request'] = self.request
        return context

    def perform_create(self, serializer):
        """Automatically assign the current user to the dataset"""
        logger.info(f"DatasetViewSet.perform_create - User ID: {self.request.user.id}")
        dataset = serializer.save(user=self.request.user)
        transaction.on_commit(lambda: ensure_dataset_work(dataset.id))

    def perform_destroy(self, instance):
        if instance.workflow_type != Dataset.WorkflowType.DWCA_CONVERSION:
            return super().perform_destroy(instance)
        files = [(source.file.storage, source.file.name) for source in instance.user_files.all() if source.file]
        conversion = getattr(instance, 'conversion', None)
        if conversion and conversion.output_file:
            files.append((conversion.output_file.storage, conversion.output_file.name))
        with transaction.atomic():
            instance.delete()
            for storage, name in files:
                transaction.on_commit(lambda storage=storage, name=name: storage.delete(name), robust=True)

    @staticmethod
    def _conversion_state(conversion, names_page=None):
        from api import conversion_tidy
        from api import conversion_names
        from api.conversion_review import state_section
        from api.dwca_conversion import option_status, validate_decisions
        plan = conversion.plan
        return {'status': conversion.status, 'plan': plan, 'decisions': conversion.decisions,
                'drop_unlinked_extension_rows': conversion.drop_unlinked_extension_rows,
                'unresolved': validate_decisions(plan, conversion.decisions, require_complete=False) if plan else [],
                'option_status': option_status(plan, conversion.decisions) if plan else {},
                'conflicts': conversion.conflicts, 'retryable': conversion.retryable, 'error': conversion.error,
                'report': {key: value for key, value in conversion_names.public_report(conversion.report).items() if key != 'row_crosswalk'},
                'updated_at': conversion.updated_at, 'download_ready': conversion.status == 'complete' and bool(conversion.output_file),
                'name_review': conversion_names.state_section(conversion, **(names_page or {})),
                'tidy': conversion_tidy.state_section(conversion),
                **state_section(conversion)}

    @staticmethod
    def _unaffected_conflicts(conversion, decisions):
        """Keep stored conflicts unless a changed choice (group choices count for their members) can remedy them."""
        previous = conversion.decisions
        changed = {key for key in {*previous, *decisions} if previous.get(key) != decisions.get(key)}
        groups = {item['id']: item['members'] for item in [*conversion.plan.get('issues', []), *conversion.plan.get('automatic_choices', [])]
                  if item.get('members')}
        changed |= {member for key in changed for member in groups.get(key, [])}
        changed |= {member['group'] for member in conversion.plan.get('row_issues', []) if member['id'] in changed}
        return [conflict for conflict in conversion.conflicts if not changed & set(conflict.get('decision_ids', []))]

    @action(detail=True, methods=['get', 'post'], url_path='conversion',
            throttle_classes=[ConversionReviewRateThrottle, ConversionChatRateThrottle])
    def conversion(self, request, *args, **kwargs):
        from api import conversion_names, conversion_review, conversion_tidy
        from api.models import DwcConversion, DwcConversionJob
        from api.dwca_conversion import validate_decisions
        from api.dwca_import import ConversionError, ImportFailure

        dataset = self.get_object()
        if dataset.workflow_type != Dataset.WorkflowType.DWCA_CONVERSION:
            raise ValidationError('This dataset is not an archive conversion.')
        if request.method == 'GET':
            query = request.query_params
            names_page = {'offset': int(query['names_offset']) if query.get('names_offset', '').isdigit() else 0,
                          'limit': int(query['names_limit']) if query.get('names_limit', '').isdigit() else 100,
                          'view': 'pending' if query.get('names_view') == 'pending' else 'all'}
            return Response(self._conversion_state(dataset.conversion, names_page))
        operation = request.data.get('action', 'convert')
        if operation not in {'convert', 'review', 'inspect', 'save', 'chat', 'names', 'check_names', 'tidy',
                             'drop_unlinked_extension_rows'}:
            raise ValidationError('Unknown conversion action.')
        with transaction.atomic():
            # Lock order: conversion, then job (docs/dwca-conversion/ai-review-and-chat.md §5.9).
            conversion = DwcConversion.objects.select_for_update().get(dataset=dataset)
            job = DwcConversionJob.objects.select_for_update().filter(conversion=conversion).first()
            ai_job = job is not None and job.action in {'review', 'chat', 'names'}
            if job is not None and not ai_job:
                return Response({'detail': 'Conversion work is already queued or running.'}, status=409)
            if operation == 'inspect':
                if ai_job:
                    conversion_review.supersede_job(conversion)
                conversion.status = 'queued'
                conversion.error = ''; conversion.save()
                DwcConversionJob.objects.create(conversion=conversion, action='inspect')
                return Response(self._conversion_state(conversion), status=202)
            if operation == 'drop_unlinked_extension_rows':
                has_unlinked_records = any(
                    conflict.get('evidence', {}).get('kind') == 'unlinked-extension-records'
                    for conflict in conversion.conflicts)
                if conversion.status != 'blocked' or not has_unlinked_records:
                    return Response({'detail': 'There are no unlinked extension records to remove.'}, status=409)
                conversion.drop_unlinked_extension_rows = True
                conversion.status = 'queued'
                conversion.error = ''
                conversion.conflicts = []
                conversion.retryable = False
                conversion.save(update_fields=['drop_unlinked_extension_rows', 'status', 'error', 'conflicts',
                                               'retryable', 'updated_at'])
                DwcConversionJob.objects.create(conversion=conversion, action='inspect')
                return Response(self._conversion_state(conversion), status=202)
            if request.data.get('plan_id') != conversion.plan.get('id'):
                return Response({'detail': 'The plan changed. Reload before submitting decisions.'}, status=409)
            if operation == 'chat':
                return self._conversion_chat(request, conversion, job)
            if conversion.status not in {'review', 'reviewing'}:
                # A completed, blocked or failed conversion's choices must stay those of its result.
                return Response({'detail': 'Choices can only be changed while the conversion is in review.'}, status=409)
            if operation == 'tidy':
                try:
                    conversion_tidy.request_changes(conversion, request.data.get('changes'))
                except ValueError as exc:
                    raise ValidationError(str(exc))
                if ai_job:
                    conversion_review.supersede_job(conversion)
                conversion.status = 'queued'
                conversion.error = ''
                conversion.save()
                DwcConversionJob.objects.create(conversion=conversion, action='replan')
                return Response(self._conversion_state(conversion), status=202)
            if operation == 'names':
                try:
                    with transaction.atomic():
                        if 'name_decisions' in request.data:
                            conversion_names.set_decisions(conversion, request.data['name_decisions'])
                        if request.data.get('bulk'):
                            conversion_names.bulk_decide(conversion, request.data['bulk'])
                        conversion_names.settle_name_questions(conversion)
                except conversion_names.NameDecisionError as exc:
                    raise ValidationError(str(exc))
                return Response(self._conversion_state(conversion))
            if operation == 'check_names':
                if job is not None:
                    return Response({'detail': 'Name checks, AI review or a reply is already running.'}, status=409)
                conversion_names.request_check(conversion, refresh=bool(request.data.get('refresh')))
                DwcConversionJob.objects.create(conversion=conversion, action='names')
                return Response(self._conversion_state(conversion), status=202)
            if operation == 'review':
                if ai_job and job.action != 'names':
                    return Response({'detail': 'AI review or a reply is already running.'}, status=409)
                if not conversion_review.ai_available():
                    raise ValidationError('AI review is unavailable because no API key is configured. You can review the choices yourself.')
                if not conversion_review.reviewable_items(conversion, manual=True):
                    return Response(self._conversion_state(conversion))
                conversion_review.review_state(conversion)['manual'] = True
                if job is not None and job.claimed_at is not None:
                    # A name check is running: it hands over to this review at its next batch boundary.
                    conversion.save(update_fields=['review', 'updated_at'])
                    return Response(self._conversion_state(conversion), status=202)
                if job is not None:
                    job.delete()  # a queued name check resumes after the review
                conversion.status = 'reviewing'
                conversion.save(update_fields=['review', 'status', 'updated_at'])
                DwcConversionJob.objects.create(conversion=conversion, action='review')
                return Response(self._conversion_state(conversion), status=202)
            # save and convert: partial `changes` (preferred, so AI choices made meanwhile survive) or a full `decisions` map.
            if 'changes' in request.data:
                changes = request.data.get('changes')
                if not isinstance(changes, dict) or any(value is not None and not isinstance(value, str) for value in changes.values()):
                    raise ValidationError('Changes must map decision IDs to option strings or null.')
            else:
                decisions = request.data.get('decisions', conversion.decisions)
                if not isinstance(decisions, dict):
                    raise ValidationError('Decisions must be a mapping of decision IDs to option strings.')
                changes = {key: decisions.get(key) for key in {*conversion.decisions, *decisions}
                           if conversion.decisions.get(key) != decisions.get(key)}
            requested = {key: value for key, value in {**conversion.decisions, **changes}.items() if value is not None}
            conflicts = self._unaffected_conflicts(conversion, requested)
            try:
                with transaction.atomic():
                    conversion_review.apply_decision_changes(
                        conversion, changes, 'user',
                        accepted_recommendation_ids=request.data.get('accepted_recommendations') or [],
                        confirm_ids=request.data.get('confirm') or [])
                    if operation == 'convert':
                        unchanged = conversion_review.blocking_conflicts(conflicts)
                        if unchanged:
                            raise ConversionError('Change one of the choices named by the last conversion attempt before converting again: '
                                                  + unchanged[0]['reason'][:300], category='decision',
                                                  decision_ids=[identifier for conflict in unchanged for identifier in conflict['decision_ids']])
                        from api.conversion_jobs import internal_retry_exhausted
                        if internal_retry_exhausted(conversion):
                            raise ConversionError('The converter failed twice with these choices because of a problem on our side. '
                                                  'Your choices are kept; converting them again will be possible after a fix '
                                                  'is released, or after you change a choice.', category='internal')
                        blockers = conversion_review.convert_blockers(conversion)
                        if blockers:
                            raise ConversionError(f'Check {len(blockers)} AI choices made before a choice they depend on changed.',
                                                  category='decision', decision_ids=blockers)
                        validate_decisions(conversion.plan, conversion.decisions, require_complete=True)
            except ImportFailure as exc:
                conversion.refresh_from_db()
                record = exc.as_conflict() if hasattr(exc, 'as_conflict') else {'reason': str(exc), 'decision_ids': []}
                return Response({'detail': str(exc), 'conflict': record}, status=400)
            conversion.conflicts = conflicts
            if operation == 'save':
                if not conversion.conflicts and conversion.status == 'review':
                    conversion.error = ''  # The stored failure has been remedied.
                conversion.save(update_fields=['conflicts', 'error', 'updated_at'])
                return Response(self._conversion_state(conversion))
            if ai_job:
                conversion_review.supersede_job(conversion)
            conversion.status = 'converting'
            conversion.error = ''; conversion.save()
            DwcConversionJob.objects.create(conversion=conversion, action='convert')
        return Response(self._conversion_state(conversion), status=202)

    def _conversion_chat(self, request, conversion, job):
        """Post a message (answered by a `chat` job) or apply confirmed proposals (no model, no job)."""
        from api import conversion_chat, conversion_review
        from api.models import DwcConversionJob
        if not conversion.plan or conversion.status not in {'review', 'reviewing', 'blocked'}:
            return Response({'detail': 'The conversation is not available in this state.'}, status=409)
        if 'confirm' in request.data:
            conversion_chat.confirm(conversion, request.data.get('confirm'), request.data.get('plan_id'))
            if job is None and conversion.status == 'review' and conversion_review.should_auto_review(conversion):
                conversion.status = 'reviewing'
                conversion.save(update_fields=['status', 'updated_at'])
                DwcConversionJob.objects.create(conversion=conversion, action='review')
            return Response(self._conversion_state(conversion))
        if not conversion_review.ai_available():
            raise ValidationError('The conversation assistant is not available. You can answer the choices in the list.')
        conversion_chat.post_user_message(conversion, request.data.get('message'))
        if job is None:
            DwcConversionJob.objects.create(conversion=conversion, action='chat')
        return Response(self._conversion_state(conversion), status=202)

    @action(detail=True, methods=['get'], url_path='conversion-download')
    def conversion_download(self, request, *args, **kwargs):
        from django.http import FileResponse
        dataset = self.get_object()
        conversion = getattr(dataset, 'conversion', None)
        if not conversion or conversion.status != 'complete' or not dataset.package_ready:
            return Response({'detail': 'A validated converted package is not ready.'}, status=409)
        prefix = 'taxonomy-data-package' if conversion.report.get('output_format') == 'taxonomy-data-package' else 'dwc-dp'
        return FileResponse(conversion.output_file.open('rb'), as_attachment=True,
                            filename=f'{prefix}-{dataset.pk}.tar.gz', content_type='application/gzip')

    @action(detail=True, methods=['get'], url_path='openai-usage')
    def openai_usage(self, request, *args, **kwargs):
        """Return internal per-call token and cost accounting for a dataset."""
        if not request.user.is_superuser:
            return Response(
                {'detail': 'OpenAI cost accounting is only available to administrators.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        from api.openai_usage import usage_summary
        from api.models import ConversionSpendReservation

        dataset = self.get_object()
        records = dataset.openai_usage_records.select_related('agent').all()
        by_stage = []
        for task_name in records.order_by().values_list('task_name', flat=True).distinct():
            stage_records = records.filter(task_name=task_name)
            by_stage.append({
                'task_name': task_name,
                **usage_summary(stage_records),
            })
        by_model = []
        for model in records.order_by().values_list('model', flat=True).distinct():
            model_records = records.filter(model=model)
            by_model.append({
                'model': model,
                **usage_summary(model_records),
            })
        by_service_tier = [
            {'service_tier': tier, **usage_summary(records.filter(service_tier=tier))}
            for tier in records.order_by().values_list('service_tier', flat=True).distinct()
        ]
        by_task_and_model = []
        groups = records.order_by().values_list('task_name', 'model').distinct()
        for task_name, model in groups:
            grouped_records = records.filter(task_name=task_name, model=model)
            by_task_and_model.append({
                'task_name': task_name,
                'model': model,
                **usage_summary(grouped_records),
            })
        return Response({
            'dataset_id': dataset.id,
            'summary': usage_summary(records),
            'by_stage': by_stage,
            'by_model': by_model,
            'by_service_tier': by_service_tier,
            'by_task_and_model': by_task_and_model,
            'requests': OpenAIUsageSerializer(records, many=True).data,
            # Conversion calls whose spend could not be confirmed stay reserved against the dataset limit.
            'conversion_reservations': [
                {'id': reservation.id, 'amount_usd': reservation.amount, 'response_id': reservation.response_id,
                 'created_at': reservation.created_at}
                for reservation in ConversionSpendReservation.objects.filter(conversion__dataset=dataset).order_by('id')],
        })

    @action(
        detail=True,
        methods=['get', 'post', 'delete'],
        url_path='attention-notification',
        throttle_classes=[AttentionNotificationRateThrottle],
    )
    def attention_notification(self, request, *args, **kwargs):
        dataset = self.get_object()

        if request.method == 'GET':
            return Response(notification_payload(dataset))

        if request.method == 'DELETE':
            cancel_notification(dataset)
            return Response(notification_payload(dataset))

        if not settings.ATTENTION_EMAIL_ENABLED:
            return Response(
                {'detail': 'Email notifications are not configured on this server.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        try:
            email = serializers.EmailField().run_validation(request.data.get('email', ''))
        except serializers.ValidationError as exc:
            raise ValidationError({'email': exc.detail})
        notification = arm_notification(dataset, email.strip())
        payload = notification_payload(dataset)
        payload['status'] = notification.status
        return Response(payload, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['get'], url_path='package-explorer')
    def package_explorer(self, request, *args, **kwargs):
        """Return the current dataset's DwC-DP resources and schema relationships."""
        from api.dwc_dp_specs import build_dwc_dp_explorer_model

        dataset = self.get_object()
        return Response(build_dwc_dp_explorer_model(dataset))

    @action(detail=True, methods=['get'])
    def tree_files(self, request, *args, **kwargs):
        """
        Fetch tree file for visualization, using dynamicProperties.phylogenies for matching.
        
        Returns tree data with occurrence counts based on phylogeny linking done by the agent.
        """
        import json
        from api.helpers.publish import parse_newick_to_tree, parse_nexus_to_tree
        
        dataset = self.get_object()
        
        # Get tree files
        all_files = dataset.user_files.all()
        tree_files = [f for f in all_files if Path(f.file.name).suffix.lower() in UserFile.TREE_EXTENSIONS]
        
        if not tree_files:
            return Response({'error': 'No tree files found for this dataset'}, status=status.HTTP_404_NOT_FOUND)
        
        user_file = tree_files[0]
        
        # Find occurrence table and build tip label -> occurrence data mapping
        occurrence_table = None
        for table in dataset.table_set.all():
            if table.title and table.title.lower().strip() == 'occurrence':
                occurrence_table = table
                break
        
        # Build mapping from tip labels to occurrence counts and coordinate availability
        tip_label_counts = {}  # tip_label -> count of occurrences
        tip_labels_with_coords = set()  # tip labels that have at least one occurrence with coordinates
        has_phylogeny_links = False
        has_coordinates = False
        
        if occurrence_table and occurrence_table.df is not None and not occurrence_table.df.empty:
            df = occurrence_table.df
            cols_lower = {str(col).lower(): col for col in df.columns}
            
            dp_col = cols_lower.get('dynamicproperties')
            lat_col = cols_lower.get('decimallatitude')
            lon_col = cols_lower.get('decimallongitude')
            has_coordinates = lat_col is not None and lon_col is not None
            
            if dp_col:
                for _, row in df.iterrows():
                    dp_val = row.get(dp_col)
                    if not dp_val or (isinstance(dp_val, str) and not dp_val.strip()):
                        continue
                    
                    try:
                        dp = json.loads(dp_val) if isinstance(dp_val, str) else dp_val
                        phylogenies = dp.get('phylogenies', [])
                        if not phylogenies:
                            continue
                        
                        has_phylogeny_links = True
                        
                        # Check if this row has valid coordinates
                        has_valid_coords = False
                        if has_coordinates:
                            try:
                                lat = row.get(lat_col)
                                lon = row.get(lon_col)
                                if lat is not None and lon is not None:
                                    lat_f = float(lat) if lat != '' else None
                                    lon_f = float(lon) if lon != '' else None
                                    if lat_f is not None and lon_f is not None and isfinite(lat_f) and isfinite(lon_f):
                                        has_valid_coords = True
                            except (ValueError, TypeError):
                                pass
                        
                        # Count each linked tip label
                        for phylo in phylogenies:
                            tip_label = phylo.get('phyloTreeTipLabel')
                            if tip_label:
                                tip_label_counts[tip_label] = tip_label_counts.get(tip_label, 0) + 1
                                if has_valid_coords:
                                    tip_labels_with_coords.add(tip_label)
                    except (json.JSONDecodeError, TypeError, AttributeError):
                        continue
        
        # Parse tree file
        try:
            file_handle = user_file.file.open('rb')
            try:
                content = file_handle.read()
                try:
                    text_content = content.decode('utf-8')
                except UnicodeDecodeError:
                    text_content = content.decode('latin-1', errors='replace')
                
                ext = Path(user_file.filename).suffix.lower()
                if ext in {'.nex', '.nexus'}:
                    tree_data = parse_nexus_to_tree(text_content)
                else:
                    tree_data = parse_newick_to_tree(text_content)
            finally:
                file_handle.close()
        except Exception as e:
            logger.error(f"Error reading tree file: {e}")
            return Response({'error': f'Error reading tree file: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
        # Decorate tree with occurrence counts from dynamicProperties linkage
        def decorate_tree(node):
            if not node:
                return None
            
            if not node.get('children') or len(node['children']) == 0:
                # Leaf node
                tip_label = node.get('name', '')
                node['original_tip_label'] = tip_label
                node['occurrence_count'] = tip_label_counts.get(tip_label, 0)
                return node
            
            # Internal node
            decorated_children = []
            for child in node.get('children', []):
                decorated_child = decorate_tree(child)
                if decorated_child:
                    decorated_children.append(decorated_child)
            
            if not decorated_children:
                return None
            
            node['children'] = decorated_children
            node['occurrence_count'] = sum(c.get('occurrence_count', 0) for c in decorated_children)
            return node
        
        decorated_tree = decorate_tree(tree_data)
        
        # Count unlinked tips
        def count_tips(node, linked=False):
            if not node:
                return 0, 0
            if not node.get('children'):
                tip_label = node.get('original_tip_label', '')
                is_linked = tip_label in tip_label_counts
                return (1 if is_linked else 0, 1)
            linked_count, total_count = 0, 0
            for child in node.get('children', []):
                l, t = count_tips(child)
                linked_count += l
                total_count += t
            return linked_count, total_count
        
        linked_tips, total_tips = count_tips(decorated_tree)
        
        response = Response({
            'tree_data': decorated_tree,
            'filename': user_file.filename,
            'file_type': user_file.file_type_label,
            'has_phylogeny_links': has_phylogeny_links,
            'has_coordinates': has_coordinates and len(tip_labels_with_coords) > 0,
            'linked_tips': linked_tips,
            'total_tips': total_tips,
        })
        response['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response['Pragma'] = 'no-cache'
        response['Expires'] = '0'
        return response
    
    @action(detail=True, methods=['post'])
    def tree_node_occurrences(self, request, *args, **kwargs):
        """
        Get occurrences linked to tree tips via dynamicProperties.phylogenies.
        
        Expects: { "tip_labels": ["tip1", "tip2", ...] }
        Returns occurrences where dynamicProperties.phylogenies[].phyloTreeTipLabel matches.
        """
        import json
        
        dataset = self.get_object()
        tip_labels = request.data.get('tip_labels', [])
        
        if not tip_labels or not isinstance(tip_labels, list):
            return Response({'error': 'tip_labels array is required'}, status=status.HTTP_400_BAD_REQUEST)
        
        tip_labels_set = set(tip_labels)
        
        # Find occurrence table
        occurrence_table = None
        for table in dataset.table_set.all():
            if table.title and table.title.lower() == 'occurrence':
                occurrence_table = table
                break
        
        if not occurrence_table:
            return Response({'error': 'No occurrence table found'}, status=status.HTTP_404_NOT_FOUND)
        
        df = occurrence_table.df
        if df is None or df.empty:
            return Response({'occurrences': []})
        
        cols_lower = {str(col).lower(): col for col in df.columns}
        dp_col = cols_lower.get('dynamicproperties')
        lat_col = cols_lower.get('decimallatitude')
        lon_col = cols_lower.get('decimallongitude')
        sci_name_col = cols_lower.get('scientificname')
        
        if not dp_col:
            return Response({'occurrences': []})
        
        matching_rows = []
        for _, row in df.iterrows():
            dp_val = row.get(dp_col)
            if not dp_val or (isinstance(dp_val, str) and not dp_val.strip()):
                continue
            
            try:
                dp = json.loads(dp_val) if isinstance(dp_val, str) else dp_val
                phylogenies = dp.get('phylogenies', [])
                
                # Check if any of this row's tip labels match the requested ones
                matched_tip = None
                for phylo in phylogenies:
                    tip_label = phylo.get('phyloTreeTipLabel')
                    if tip_label and tip_label in tip_labels_set:
                        matched_tip = tip_label
                        break
                
                if not matched_tip:
                    continue
                
                # Build occurrence response
                occ = {'tipLabel': matched_tip}
                
                # Add coordinates if available
                if lat_col and lon_col:
                    try:
                        lat_val = row.get(lat_col)
                        lon_val = row.get(lon_col)
                        if lat_val is not None and lon_val is not None:
                            occ['decimalLatitude'] = float(lat_val) if lat_val != '' else None
                            occ['decimalLongitude'] = float(lon_val) if lon_val != '' else None
                    except (ValueError, TypeError):
                        pass
                
                # Add scientificName if available
                if sci_name_col:
                    sci_name = row.get(sci_name_col)
                    if sci_name and str(sci_name).strip() and str(sci_name).lower() != 'nan':
                        occ['scientificName'] = str(sci_name).strip()
                
                # Add common identifier fields
                for field in ['occurrenceID', 'catalogNumber', 'recordNumber']:
                    col = cols_lower.get(field.lower())
                    if col:
                        val = row.get(col)
                        if val is not None and str(val).strip() and str(val).lower() != 'nan':
                            occ[field] = str(val).strip()
                
                matching_rows.append(occ)
                
            except (json.JSONDecodeError, TypeError, AttributeError):
                continue
        
        return Response({'occurrences': matching_rows})


class TableViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = TableSerializer
    permission_classes = [IsAuthenticatedOrSuperuser]
    filterset_fields = ['dataset', 'title']
    ordering = ['-updated_at']
    http_method_names = ['get', 'head', 'options']

    def get_queryset(self):
        """Return lightweight, user-visible table records without loading DataFrames."""
        queryset = Table.objects.exclude(title__endswith='_tmp').defer('df').order_by('-updated_at', '-id')
        if self.request.user.is_superuser:
            return queryset
        return queryset.filter(dataset__user=self.request.user)

    @action(detail=True, methods=['get'])
    def rows(self, request, *args, **kwargs):
        """Return one bounded row page for a table."""
        query = TablePageQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        table = self.get_object()
        offset = query.validated_data['offset']
        limit = query.validated_data['limit']
        search = query.validated_data.get('search', '')
        rows = None
        if search:
            try:
                rows = table.search_rows(
                    search,
                    column=query.validated_data.get('column'),
                    exact=query.validated_data['exact'],
                )
            except ValueError as exc:
                return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({
            'count': table.row_count if rows is None else len(rows.index),
            'total': table.row_count,
            'offset': offset,
            'limit': limit,
            'columns': table.columns,
            'results': table.row_page(offset, limit, rows),
        })


class UserFileViewSet(viewsets.ModelViewSet):
    serializer_class = UserFileSerializer
    permission_classes = [IsAuthenticatedOrSuperuser]
    filterset_fields = ['dataset']
    http_method_names = ['get', 'post', 'delete', 'head', 'options']

    def get_queryset(self):
        queryset = UserFile.objects.all().order_by('-uploaded_at', '-id')
        dataset_id = self.request.query_params.get('dataset')
        if dataset_id:
            queryset = queryset.filter(dataset_id=dataset_id)

        if self.request.user.is_superuser:
            return queryset

        return queryset.filter(dataset__user=self.request.user)

    def perform_create(self, serializer):
        dataset_id = self.request.data.get('dataset') or self.request.data.get('dataset_id')
        if not dataset_id:
            raise ValidationError({'dataset': 'Dataset is required.'})

        try:
            if self.request.user.is_superuser:
                dataset = Dataset.objects.get(id=dataset_id)
            else:
                dataset = Dataset.objects.get(id=dataset_id, user=self.request.user)
        except Dataset.DoesNotExist:
            raise ValidationError({'dataset': 'Dataset not found or not accessible.'})

        # Ensure user cannot override dataset assignment
        if hasattr(serializer, 'validated_data'):
            serializer.validated_data.pop('dataset', None)
        serializer.save(dataset=dataset)

    def perform_destroy(self, instance):
        dataset = instance.dataset
        if dataset.workflow_type == Dataset.WorkflowType.DWCA_CONVERSION:
            raise ValidationError('Conversion source files are immutable. Delete the conversion or start a new one.')
        file_type = instance.file_type
        file_name = instance.file.name
        storage = instance.file.storage
        with transaction.atomic():
            instance.delete()
            if file_type == UserFile.FileType.TABULAR:
                dataset.table_set.all().delete()
                dataset.rebuild_tables_from_user_files()
            dataset.handle_source_change()
            transaction.on_commit(lambda: storage.delete(file_name), robust=True)


class TaxonNameMatchViewSet(viewsets.ReadOnlyModelViewSet):
    """Taxon labels awaiting or holding a reviewer decision. Filter with ?dataset=<id>."""

    serializer_class = TaxonNameMatchSerializer
    permission_classes = [IsAuthenticatedOrSuperuser]
    filterset_fields = ['dataset', 'decision', 'source_table', 'context_column']

    def get_queryset(self):
        queryset = TaxonNameMatch.objects.filter(record_count__gt=0)
        if self.request.user.is_superuser:
            return queryset
        return queryset.filter(dataset__user=self.request.user)

    @action(detail=True, methods=['post'])
    def decide(self, request, *args, **kwargs):
        from api import taxon_matching

        row = self.get_object()
        payload = TaxonDecisionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        # Decisions and Done take the same dataset lock, so a decision cannot land after the
        # review has been applied.
        with transaction.atomic():
            Dataset.objects.select_for_update().get(pk=row.dataset_id)
            review = taxon_matching.open_review(row.dataset)
            if not review or review[1] != {'source_table': row.source_table, 'context_column': row.context_column}:
                return self._review_closed()
            row.refresh_from_db()
            try:
                taxon_matching.decide(
                    row,
                    data['decision'],
                    user=request.user,
                    usage_id=data.get('usage_id'),
                    confirm_coarser=data.get('confirm_coarser', False),
                    name={
                        key: data.get(key)
                        for key in ('scientificName', 'scientificNameAuthorship', 'taxonRank')
                    },
                )
            except ValueError as exc:
                raise ValidationError({'detail': str(exc)})
            except taxon_matching.TaxonServiceError as exc:
                return Response({'detail': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(self.get_serializer(row).data)

    @staticmethod
    def _review_closed():
        return Response(
            {'detail': 'ChatIPT is not waiting for decisions on these names. Your earlier decisions are saved.'},
            status=status.HTTP_409_CONFLICT,
        )

    @action(detail=False, methods=['post'], url_path='accept-exact')
    def accept_exact(self, request, *args, **kwargs):
        """Accept pending EXACT matches of names written in the label, for one dataset."""
        from api import taxon_matching

        dataset_id = request.data.get('dataset')
        datasets = Dataset.objects.all() if request.user.is_superuser else Dataset.objects.filter(user=request.user)
        with transaction.atomic():
            dataset = get_object_or_404(datasets.select_for_update(), pk=dataset_id)
            review = taxon_matching.open_review(dataset)
            if not review:
                return self._review_closed()
            accepted = taxon_matching.accept_exact_matches(dataset, user=request.user, **review[1])
        return Response({
            'accepted': len(accepted),
            'records': sum(row.record_count for row in accepted),
            'results': self.get_serializer(accepted, many=True).data,
        })

    @action(detail=False, methods=['post'], url_path='finish-review')
    def finish_review(self, request, *args, **kwargs):
        """Apply a finished taxon review, then hand the outcome back to the paused agent.

        Applying is deterministic, so ChatIPT does it here rather than spending an agent turn on
        it. If it cannot, the note asks the agent to apply with ApplyTaxonDecisions instead, so the
        user is never left waiting.
        """
        from api import taxon_matching

        agents = Agent.objects.all() if request.user.is_superuser else Agent.objects.filter(dataset__user=request.user)
        requested = get_object_or_404(agents, pk=request.data.get('agent'))
        with transaction.atomic():
            # Dataset before agent: the same lock order as the agent turn worker.
            dataset = Dataset.objects.select_for_update().get(pk=requested.dataset_id)
            review = taxon_matching.open_review(dataset, lock=True)
            if not review or review[0].id != requested.id:
                return Response(
                    {'detail': 'ChatIPT is not waiting for this taxon review.'},
                    status=status.HTTP_409_CONFLICT,
                )
            agent, scope = review
            source_table = scope['source_table']
            context_column = scope['context_column']
            rows = list(TaxonNameMatch.objects.filter(
                dataset=agent.dataset,
                source_table=source_table,
                context_column=context_column,
                record_count__gt=0,
            ))
            try:
                table, result = taxon_matching.apply_review(agent.dataset, source_table, context_column)
                applied = True
                note = (
                    "ChatIPT applied the reviewed taxon names itself; do not call ApplyTaxonDecisions for "
                    f"this review. {taxon_matching.describe_apply_result(result, table)} "
                    "Re-run ValidateDwcDp and record remaining taxonomic limitations in structure_notes."
                )
            except taxon_matching.ReviewApplyError as exc:
                applied = False
                note = (
                    f"The reviewed taxon names could not be applied automatically ({exc}). Call "
                    f"ApplyTaxonDecisions for the `{source_table}` table"
                    + (f" with context column `{context_column}`" if context_column else "")
                    + ", then re-run ValidateDwcDp."
                )
            message = Message.objects.create(
                agent=agent,
                openai_obj={
                    'role': Message.Role.USER,
                    'content': f"{taxon_matching.review_summary(rows)}\n\n[NOTE: {note}]",
                },
            )
            transaction.on_commit(lambda: ensure_dataset_work(agent.dataset_id))
        return Response({'applied': applied, 'message': MessageSerializer(message).data})

    @action(detail=False, methods=['get'])
    def search(self, request, *args, **kwargs):
        """COL XR name suggestions for a manual choice."""
        from api import taxon_matching

        text = (request.query_params.get('q') or '').strip()
        if len(text) < 3:
            return Response({'results': []})
        try:
            return Response({'results': taxon_matching.search_col(text)})
        except taxon_matching.TaxonServiceError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)


class TaskViewSet(viewsets.ModelViewSet):
    serializer_class = TaskSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = '__all__'

    def get_queryset(self):
        """Return all tasks (they are system-wide, not user-specific)"""
        return Task.objects.all()


class MessageViewSet(viewsets.ModelViewSet):
    serializer_class = MessageSerializer
    permission_classes = [IsAuthenticatedOrSuperuser]
    filterset_fields = ['agent', 'created_at']

    def get_queryset(self):
        """Filter messages to only show those belonging to the authenticated user's datasets, unless user is superuser"""
        # Superusers can see all messages
        if self.request.user.is_superuser:
            return Message.objects.all()
        
        # Regular users only see their own messages
        return Message.objects.filter(agent__dataset__user=self.request.user)

    def perform_create(self, serializer):
        agent = serializer.validated_data.get('agent')
        openai_obj = serializer.validated_data.get('openai_obj') or {}

        if agent and (openai_obj or {}).get('role') == Message.Role.USER:
            dataset = agent.dataset
            previous_message = agent.message_set.order_by('-created_at').first()
            cutoff = previous_message.created_at if previous_message else None

            new_files_qs = dataset.user_files.order_by('uploaded_at', 'id')
            if cutoff:
                new_files_qs = new_files_qs.filter(uploaded_at__gt=cutoff)
            new_files = list(new_files_qs)

            new_tables_qs = dataset.table_set.order_by('created_at', 'id')
            if cutoff:
                new_tables_qs = new_tables_qs.filter(created_at__gt=cutoff)
            new_tables = list(new_tables_qs)

            note_sections = []
            if new_files:
                file_descriptions = []
                pdf_attachments = list(openai_obj.get('pdf_attachments') or [])
                for user_file in new_files:
                    description = user_file.filename
                    if user_file.file_type == UserFile.FileType.TREE:
                        preview = self._read_tree_preview(user_file)
                        if preview:
                            description = f"{description} (preview: {preview})"
                    elif user_file.file_type == UserFile.FileType.PDF:
                        description = f"{description} (attached to the model as a PDF file input)"
                        pdf_attachments.append({
                            'user_file_id': user_file.id,
                            'filename': user_file.filename,
                        })
                    file_descriptions.append(description)
                note_sections.append(f"User uploaded file(s): {', '.join(file_descriptions)}")
                if pdf_attachments:
                    openai_obj['pdf_attachments'] = pdf_attachments

            if new_tables:
                note_sections.append("New table ids: [" + ", ".join(str(table.id) for table in new_tables) + "]")

            if note_sections:
                note = "[NOTE: " + "; ".join(note_sections) + "]"
                content = (openai_obj.get('content') or '').strip()
                openai_obj['content'] = f"{content}\n\n{note}" if content else note
                openai_obj['role'] = Message.Role.USER
                serializer.validated_data['openai_obj'] = openai_obj

        message = serializer.save()
        if message.role == Message.Role.USER:
            transaction.on_commit(lambda: ensure_dataset_work(message.agent.dataset_id))

    @staticmethod
    def _read_tree_preview(user_file, max_chars=200):
        try:
            user_file.file.open('rb')
            raw = user_file.file.read(max_chars * 4)
        except Exception:
            return ''
        finally:
            try:
                user_file.file.close()
            except Exception:
                pass

        text = raw.decode('utf-8', errors='replace')
        compact = " ".join(text.split())
        return compact[:max_chars]


class AgentViewSet(viewsets.ModelViewSet):
    serializer_class = AgentSerializer
    permission_classes = [IsAuthenticatedOrSuperuser]
    filterset_fields = ['created_at', 'completed_at', 'dataset', 'task']

    def get_queryset(self):
        """Filter agents to only show those belonging to the authenticated user's datasets, unless user is superuser"""
        # Superusers can see all agents
        if self.request.user.is_superuser:
            return Agent.objects.all()
        
        # Regular users only see their own agents
        return Agent.objects.filter(dataset__user=self.request.user)
    
    def create(self, request, *args, **kwargs):
        """Create a new agent with resume vs create-new + digest logic"""
        dataset_id = request.data.get('dataset')
        task_id = request.data.get('task')
        
        if not dataset_id or not task_id:
            return Response(
                {'error': 'Both dataset and task are required'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            # Superusers can access any dataset, regular users only their own
            if request.user.is_superuser:
                dataset = Dataset.objects.get(id=dataset_id)
            else:
                dataset = Dataset.objects.get(id=dataset_id, user=request.user)
            task = Task.objects.get(id=task_id)
        except (Dataset.DoesNotExist, Task.DoesNotExist):
            return Response(
                {'error': 'Dataset or task not found'}, 
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Rule 1: If an agent is in progress → resume it
        active_agent = dataset.agent_set.filter(completed_at__isnull=True).first()
        if active_agent:
            serializer = self.get_serializer(active_agent)
            return Response(serializer.data, status=status.HTTP_200_OK)
        
        # Rule 2: Else → create a new agent with state digest
        agent = task.create_agent_with_system_messages(dataset=dataset)
        serializer = self.get_serializer(agent)
        return Response(serializer.data, status=status.HTTP_201_CREATED)
