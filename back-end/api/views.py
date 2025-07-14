from rest_framework import viewsets, status
from api.serializers import DatasetSerializer, TableSerializer, MessageSerializer, AgentSerializer, TaskSerializer
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
@permission_classes([IsAuthenticated])
def user_profile(request):
    """Get current user's profile information"""
    serializer = UserSerializer(request.user)
    return Response(serializer.data)


@api_view(['GET'])
@permission_classes([AllowAny])
def orcid_login(request):
    """Redirect to ORCID OAuth2 authorization endpoint"""
    client_id = settings.SOCIALACCOUNT_PROVIDERS['orcid']['APP']['client_id']
    # Callback should go to backend, not frontend
    base_url = request.build_absolute_uri('/').rstrip('/')
    redirect_uri = f"{base_url}/api/auth/orcid/callback/"
    
    # ORCID OAuth2 authorization URL
    auth_url = "https://orcid.org/oauth/authorize"
    
    # OAuth2 parameters - expanded scope to get more user info
    params = {
        'client_id': client_id,
        'response_type': 'code',
        'scope': 'openid',
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
        token_url = "https://orcid.org/oauth/token"
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
        user_info_url = "https://orcid.org/oauth/userinfo"
        headers = {'Authorization': f'Bearer {access_token}'}
        
        user_response = requests.get(user_info_url, headers=headers)
        user_response.raise_for_status()
        user_info = user_response.json()
        
        # Get ORCID ID from userinfo
        orcid_id = user_info.get('sub')
        if not orcid_id:
            logger.error(f"ORCID userinfo response: {user_info}")
            return redirect(f"{settings.FRONTEND_URL}?error=no_orcid_id")
        
        # Get detailed user info from ORCID member API
        member_url = f"https://pub.orcid.org/v3.0/{orcid_id}"
        member_headers = {
            'Authorization': f'Bearer {access_token}',
            'Accept': 'application/json'
        }
        
        member_response = requests.get(member_url, headers=member_headers)
        member_response.raise_for_status()
        member_data = member_response.json()
        
        # Extract email from member data
        email = None
        if 'person' in member_data and 'emails' in member_data['person']:
            emails = member_data['person']['emails']['email']
            if emails:
                email = emails[0].get('email')
        
        # If no email in member data, try to get it from userinfo
        if not email:
            email = user_info.get('email')
        
        # If still no email, create a placeholder email
        if not email:
            email = f"{orcid_id}@orcid.org"
        
        logger.info(f"ORCID ID: {orcid_id}")
        logger.info(f"Email: {email}")
        logger.info(f"User info: {user_info}")
        logger.info(f"Member data keys: {list(member_data.keys()) if member_data else 'No member data'}")
        
        user, created = User.objects.get_or_create(
            email=email,
            defaults={
                'username': email,
                'orcid_id': orcid_id,
                'orcid_access_token': access_token,
                'orcid_refresh_token': token_info.get('refresh_token', ''),
                'is_active': True,  # Ensure user is active
            }
        )
        
        if not created:
            # Update existing user's ORCID info
            user.orcid_id = orcid_id
            user.orcid_access_token = access_token
            user.orcid_refresh_token = token_info.get('refresh_token', '')
            user.is_active = True  # Ensure user is active
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

        # Do a refresh of agents and messages, so we get the next one of each if necessary
        next_agent = dataset.next_agent()
        if next_agent:
            next_agent.next_message()
            dataset.refresh_from_db()

        serializer = self.get_serializer(dataset)
        return Response(serializer.data)


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
