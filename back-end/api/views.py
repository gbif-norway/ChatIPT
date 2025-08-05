from rest_framework import viewsets, status
from api.serializers import DatasetSerializer, DatasetListSerializer, TableSerializer, MessageSerializer, AgentSerializer, TaskSerializer
from api.models import Dataset, Table, Message, Agent, Task
from rest_framework.response import Response
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import IsAuthenticated, AllowAny
from django.shortcuts import get_object_or_404, redirect
from django.contrib.auth import get_user_model
from rest_framework.serializers import ModelSerializer
from django.conf import settings
import requests
from urllib.parse import urlencode
import logging

logger = logging.getLogger(__name__)

User = get_user_model()


class UserSerializer(ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'email', 'first_name', 'last_name', 'orcid_id', 'institution', 'department', 'country']
        read_only_fields = ['id', 'email', 'orcid_id']


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
    
    # OAuth2 parameters - using public API scopes only
    params = {
        'client_id': client_id,
        'response_type': 'code',
        'scope': 'openid /authenticate',
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
        
        logger.info(f"Token response status: {response.status_code}")
        logger.info(f"Token info keys: {list(token_info.keys())}")
        logger.info(f"Access token present: {'access_token' in token_info}")
        
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
        public_url = f"{get_orcid_url('public_api')}/{orcid_id}"
        public_headers = {
            'Accept': 'application/json'
        }
        
        logger.info(f"Fetching public profile data from: {public_url}")
        public_response = requests.get(public_url, headers=public_headers)
        logger.info(f"Public API response status: {public_response.status_code}")
        
        if public_response.status_code != 200:
            logger.error(f"Public API error: {public_response.text}")
            # Try alternative endpoint
            public_url = f"{get_orcid_url('public_api_record')}/{orcid_id}/record"
            logger.info(f"Trying alternative endpoint: {public_url}")
            public_response = requests.get(public_url, headers=public_headers)
            logger.info(f"Alternative endpoint response status: {public_response.status_code}")
        
        public_response.raise_for_status()
        public_data = public_response.json()
        
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
                    first_name = name['given-names'].get('value', '')
                if 'family-name' in name:
                    last_name = name['family-name'].get('value', '')
            
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
        logger.info(f"User info: {user_info}")
        logger.info(f"Public data keys: {list(public_data.keys()) if public_data else 'No public data'}")
        
        # Debug employment data structure
        if 'person' in public_data and 'employments' in public_data['person']:
            logger.info(f"Employment data structure: {public_data['person']['employments']}")
        
        user, created = User.objects.get_or_create(
            email=email,
            defaults={
                'username': email,
                'orcid_id': orcid_id,
                'orcid_access_token': access_token,
                'orcid_refresh_token': token_info.get('refresh_token', ''),
                'first_name': first_name or '',
                'last_name': last_name or '',
                'institution': institution or '',
                'department': department or '',
                'country': country or '',
                'is_active': True,  # Ensure user is active
            }
        )
        
        if not created:
            # Update existing user's ORCID info and profile data
            user.orcid_id = orcid_id
            user.orcid_access_token = access_token
            user.orcid_refresh_token = token_info.get('refresh_token', '')
            user.is_active = True  # Ensure user is active
            
            # Update profile information if available
            if first_name:
                user.first_name = first_name
            if last_name:
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
    datasets = Dataset.objects.filter(user=request.user).order_by('-created_at')
    serializer = DatasetListSerializer(datasets, many=True)
    return Response(serializer.data)


class DatasetViewSet(viewsets.ModelViewSet):
    serializer_class = DatasetSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ['created_at', 'orcid']

    def get_queryset(self):
        """Filter datasets to only show those belonging to the authenticated user"""
        logger.info(f"DatasetViewSet.get_queryset - User authenticated: {self.request.user.is_authenticated}")
        logger.info(f"User ID: {self.request.user.id if self.request.user.is_authenticated else 'None'}")
        logger.info(f"Session ID: {self.request.session.session_key}")
        return Dataset.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        """Automatically assign the current user to the dataset"""
        logger.info(f"DatasetViewSet.perform_create - User ID: {self.request.user.id}")
        serializer.save(user=self.request.user)

    # @action(detail=True)
    # def next_agent(self, request, *args, **kwargs):
    #     dataset = self.get_object()
    #     serializer = AgentSerializer(dataset.next_agent())
    #     return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True)
    def refresh(self, request, *args, **kwargs):
        dataset = self.get_object()

        try:
            # Do a refresh of agents and messages, so we get the next one of each if necessary
            next_agent = dataset.next_agent()
            if next_agent:
                next_agent.next_message()
                dataset.refresh_from_db()

            serializer = self.get_serializer(dataset)
            return Response(serializer.data)
        except Exception as e:
            logger.error(f"Error refreshing dataset {dataset.id}: {e}")
            return Response(
                {'error': f'Failed to refresh dataset: {str(e)}'}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class TableViewSet(viewsets.ModelViewSet):
    serializer_class = TableSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ['dataset', 'title']
    ordering = ['-updated_at']

    def get_queryset(self):
        """Filter tables to only show those belonging to the authenticated user's datasets"""
        return Table.objects.filter(dataset__user=self.request.user)


class TaskViewSet(viewsets.ModelViewSet):
    serializer_class = TaskSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = '__all__'

    def get_queryset(self):
        """Return all tasks (they are system-wide, not user-specific)"""
        return Task.objects.all()


class MessageViewSet(viewsets.ModelViewSet):
    serializer_class = MessageSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ['agent', 'created_at']

    def get_queryset(self):
        """Filter messages to only show those belonging to the authenticated user's datasets"""
        return Message.objects.filter(agent__dataset__user=self.request.user)


class AgentViewSet(viewsets.ModelViewSet):
    serializer_class = AgentSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ['created_at', 'completed_at', 'dataset', 'task']

    def get_queryset(self):
        """Filter agents to only show those belonging to the authenticated user's datasets"""
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
