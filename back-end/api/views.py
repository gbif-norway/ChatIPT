from rest_framework import viewsets, status
from rest_framework.exceptions import ValidationError
from api.serializers import DatasetSerializer, DatasetListSerializer, TableSerializer, MessageSerializer, AgentSerializer, TaskSerializer, UserFileSerializer
from api.models import Dataset, Table, Message, Agent, Task, UserFile
from rest_framework.response import Response
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import IsAuthenticated, AllowAny, BasePermission
from django.shortcuts import get_object_or_404, redirect
from django.contrib.auth import get_user_model
from rest_framework.serializers import ModelSerializer
from django.conf import settings
import requests
from urllib.parse import urlencode
from collections import defaultdict
import logging
import re
from math import isfinite

logger = logging.getLogger(__name__)

User = get_user_model()


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

    @action(detail=True, methods=['get'])
    def tree_files(self, request, *args, **kwargs):
        """Fetch tree file contents for visualization, showing all nodes without filtering"""
        import time
        start_time = time.time()
        logger.info(f"tree_files endpoint called for dataset {kwargs.get('pk')}")
        
        dataset = self.get_object()
        
        # Get all tree files for this dataset by checking file extensions
        from pathlib import Path
        from api.helpers.publish import (
            parse_newick_to_tree, parse_nexus_to_tree, 
            parse_newick_tip_labels, parse_nexus_tip_labels,
            match_tip_label_to_scientific_name
        )
        import pandas as pd
        from collections import defaultdict
        
        all_files = dataset.user_files.all()
        tree_files = [f for f in all_files if Path(f.file.name).suffix.lower() in UserFile.TREE_EXTENSIONS]
        
        if not tree_files:
            return Response(
                {'error': 'No tree files found for this dataset'}, 
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Find occurrence table (optional)
        occurrence_table = None
        all_tables = list(dataset.table_set.all())
        logger.info(f"Found {len(all_tables)} tables for dataset {dataset.id}")
        for table in all_tables:
            logger.info(f"Table: id={table.id}, title='{table.title}'")
            if table.title and table.title.lower().strip() == 'occurrence':
                occurrence_table = table
                logger.info(f"Found occurrence table: id={table.id}")
                break
        
        # Check if we have occurrence data with scientificName and coordinates
        has_occurrence_data = False
        has_scientific_name = False
        has_coordinates = False
        tip_label_to_sci_name = {}
        matched_scientific_names = set()
        scientific_names_with_counts = {}
        all_scientific_names = set()
        standardized_columns = {}
        scientific_names_with_coordinates = set()  # Initialize to empty set
        
        if occurrence_table:
            df = occurrence_table.df
            if df is not None and not df.empty:
                has_occurrence_data = True
                standardized_columns = {str(col).lower(): col for col in df.columns}
                logger.info(f"Occurrence table columns (lowercase): {list(standardized_columns.keys())}")
                sci_name_col = standardized_columns.get('scientificname', standardized_columns.get('scientific_name'))
                has_lat = 'decimallatitude' in standardized_columns
                has_long = 'decimallongitude' in standardized_columns
                has_coordinates = has_lat and has_long
                logger.info(f"Coordinate check: has_lat={has_lat}, has_long={has_long}, has_coordinates={has_coordinates}")
                
                if sci_name_col is not None:
                    has_scientific_name = True
                    # Get all unique scientific names and count occurrences
                    scientific_name_series = df[sci_name_col].astype(str).str.strip()
                    valid_scientific_names = scientific_name_series[
                        (scientific_name_series != '') &
                        (scientific_name_series.str.lower() != 'nan')
                    ]
                    scientific_names_with_counts = valid_scientific_names.value_counts().to_dict()
                    all_scientific_names = set(scientific_names_with_counts.keys())
                else:
                    # No scientific names available
                    scientific_names_with_coordinates = set()
                
                # If coordinates are available, build a set of scientific names that have coordinates
                if has_coordinates and has_scientific_name and sci_name_col:
                    scientific_names_with_coordinates = set()
                    lat_col = standardized_columns.get('decimallatitude')
                    lon_col = standardized_columns.get('decimallongitude')
                    if lat_col and lon_col:
                        # Build set of scientific names that have at least one occurrence with valid coordinates
                        for idx, row in df.iterrows():
                            sci_name = str(row.get(sci_name_col, '')).strip()
                            if not sci_name or sci_name.lower() == 'nan':
                                continue
                            try:
                                lat_val = row.get(lat_col)
                                lon_val = row.get(lon_col)
                                if lat_val is not None and lon_val is not None:
                                    lat = float(lat_val) if lat_val != '' else None
                                    lon = float(lon_val) if lon_val != '' else None
                                    if lat is not None and lon is not None and isfinite(lat) and isfinite(lon):
                                        scientific_names_with_coordinates.add(sci_name)
                            except (ValueError, TypeError):
                                pass
                elif not has_scientific_name:
                    # Initialize empty set if no scientific names
                    scientific_names_with_coordinates = set()
            else:
                logger.warning(f"Occurrence table {occurrence_table.id} has empty or None dataframe")
        else:
            logger.warning(f"No occurrence table found for dataset {dataset.id}. Available tables: {[t.title for t in all_tables]}")
        
        # For now, use the first tree file
        user_file = tree_files[0]
        logger.info(f"Processing tree file {user_file.id} ({user_file.filename})")
        try:
            # Try to open the file - this will work with both FileSystemStorage and MinIOStorage
            open_start = time.time()
            try:
                file_handle = user_file.file.open('rb')
                logger.info(f"File opened in {time.time() - open_start:.2f}s")
            except Exception as open_error:
                logger.error(f"Error opening tree file {user_file.id} ({user_file.filename}): {open_error}")
                logger.error(f"File storage backend: {type(user_file.file.storage).__name__}")
                logger.error(f"File path: {user_file.file.name}")
                raise
            
            try:
                read_start = time.time()
                content = file_handle.read()
                logger.info(f"File read ({len(content)} bytes) in {time.time() - read_start:.2f}s")
                try:
                    text_content = content.decode('utf-8')
                except UnicodeDecodeError:
                    text_content = content.decode('latin-1', errors='replace')
                
                # Parse tree structure
                parse_start = time.time()
                ext = Path(user_file.filename).suffix.lower()
                try:
                    if ext in {'.nex', '.nexus'}:
                        tree_data = parse_nexus_to_tree(text_content)
                        tip_labels = parse_nexus_tip_labels(text_content)
                    else:
                        tree_data = parse_newick_to_tree(text_content)
                        tip_labels = parse_newick_tip_labels(text_content)
                    logger.info(f"Tree parsed ({len(tip_labels)} tips) in {time.time() - parse_start:.2f}s")
                except Exception as parse_error:
                    logger.error(f"Error parsing tree file {user_file.id}: {parse_error}")
                    return Response(
                        {'error': f'Error parsing tree file: {str(parse_error)}'}, 
                        status=status.HTTP_400_BAD_REQUEST
                    )
                
                # If we have scientific names, try to match them (but don't filter the tree)
                if has_scientific_name and all_scientific_names:
                    def tokenize_label(value: str) -> list[str]:
                        if not value:
                            return []
                        return [token for token in re.split(r'[^a-z0-9]+', value.lower()) if token]

                    # Fast lookup: map genus+species tokens from the tree tips
                    tip_lookup = defaultdict(list)
                    for tip_label in tip_labels:
                        tokens = tokenize_label(tip_label)
                        if len(tokens) >= 2:
                            tip_lookup[tokens[0] + tokens[1]].append(tip_label)

                    sci_name_keys = {}
                    for sci_name in all_scientific_names:
                        tokens = tokenize_label(sci_name)
                        if len(tokens) >= 2:
                            sci_name_keys[sci_name] = tokens[0] + tokens[1]
                        else:
                            sci_name_keys[sci_name] = None

                    # First pass: try to match using the precomputed keys
                    for sci_name, key in sci_name_keys.items():
                        if not key:
                            continue
                        matching_tips = tip_lookup.get(key)
                        if not matching_tips:
                            continue

                        matched_here = False
                        for tip_label in matching_tips:
                            if tip_label in tip_label_to_sci_name:
                                continue
                            if match_tip_label_to_scientific_name(tip_label, sci_name):
                                tip_label_to_sci_name[tip_label] = sci_name
                                matched_here = True
                        if matched_here:
                            matched_scientific_names.add(sci_name)

                    # Fallback: only iterate over the remaining unmatched items
                    remaining_tip_labels = [tip for tip in tip_labels if tip not in tip_label_to_sci_name]
                    if remaining_tip_labels:
                        remaining_scientific_names = [sci for sci in all_scientific_names if sci not in matched_scientific_names]
                        for tip_label in remaining_tip_labels:
                            for sci_name in remaining_scientific_names:
                                if match_tip_label_to_scientific_name(tip_label, sci_name):
                                    tip_label_to_sci_name[tip_label] = sci_name
                                    matched_scientific_names.add(sci_name)
                                    break
                
                # Decorate tree with scientific names and occurrence counts
                # When coordinates are available, filter to only show nodes with coordinate data
                # When coordinates are not available, show all nodes
                def decorate_tree_with_occurrences(node):
                    """Recursively decorate tree with scientific names and occurrence counts.
                    If coordinates are available, only keep nodes that match scientific names with coordinates.
                    If coordinates are not available, keep all nodes."""
                    if not node:
                        return None
                    
                    node_copy = node.copy()
                    
                    # If it's a leaf node
                    if not node.get('children') or len(node['children']) == 0:
                        original_name = node.get('name', '')
                        # Check if this tip label matches any scientific name
                        matched_sci_name = tip_label_to_sci_name.get(original_name)
                        
                        # If coordinates are available, only keep nodes that match scientific names with coordinates
                        if has_coordinates and has_scientific_name:
                            if not matched_sci_name or matched_sci_name not in scientific_names_with_coordinates:
                                return None  # Filter out nodes without coordinates
                        
                        if matched_sci_name:
                            # Add scientific name but keep original name too
                            node_copy['name'] = original_name  # Keep original tip label
                            node_copy['scientific_name'] = matched_sci_name  # Add matched scientific name
                            node_copy['original_tip_label'] = original_name  # Keep original for reference
                            node_copy['occurrence_count'] = scientific_names_with_counts.get(matched_sci_name, 0)
                        else:
                            # If coordinates are available, we've already filtered these out above
                            # So if we reach here, coordinates are not available - keep the node
                            node_copy['name'] = original_name
                            node_copy['original_tip_label'] = original_name
                            node_copy['occurrence_count'] = 0
                        return node_copy
                    else:
                        # Internal node: process children first
                        decorated_children = []
                        for child in node.get('children', []):
                            decorated_child = decorate_tree_with_occurrences(child)
                            if decorated_child is not None:
                                decorated_children.append(decorated_child)
                        
                        # Only keep internal node if it has children after filtering
                        if not decorated_children:
                            return None
                        
                        node_copy['children'] = decorated_children
                        # Aggregate occurrence counts
                        node_copy['occurrence_count'] = sum(
                            child.get('occurrence_count', 0) for child in decorated_children
                        )
                        return node_copy
                
                decorated_tree = decorate_tree_with_occurrences(tree_data)
                
                # Get unmatched scientific names (if we had scientific names to match)
                unmatched_scientific_names = []
                total_unique_scientific_names = 0
                if has_scientific_name:
                    unmatched_scientific_names = sorted(list(all_scientific_names - matched_scientific_names))
                    total_unique_scientific_names = len(all_scientific_names)
                
                total_time = time.time() - start_time
                logger.info(f"tree_files endpoint completed in {total_time:.2f}s")
                
                response = Response({
                    'tree_data': decorated_tree,
                    'filename': user_file.filename,
                    'file_type': user_file.file_type_label,
                    'unmatched_scientific_names': unmatched_scientific_names,
                    'total_unique_scientific_names': total_unique_scientific_names,
                    'has_coordinates': has_coordinates,
                    'has_scientific_name': has_scientific_name,
                })
                # Prevent caching to ensure fresh data on each request
                response['Cache-Control'] = 'no-cache, no-store, must-revalidate'
                response['Pragma'] = 'no-cache'
                response['Expires'] = '0'
                return response
            finally:
                if 'file_handle' in locals():
                    file_handle.close()
                elif hasattr(user_file, 'file') and hasattr(user_file.file, 'close'):
                    user_file.file.close()
        except Exception as e:
            logger.error(f"Error reading tree file {user_file.id}: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return Response(
                {'error': f'Error reading file: {str(e)}'}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=True, methods=['post'])
    def tree_node_occurrences(self, request, *args, **kwargs):
        """Get occurrences for tree node based on tip labels"""
        dataset = self.get_object()
        
        tip_labels = request.data.get('tip_labels', [])
        if not tip_labels or not isinstance(tip_labels, list):
            return Response(
                {'error': 'tip_labels array is required'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Find occurrence table
        occurrence_table = None
        for table in dataset.table_set.all():
            if table.title and table.title.lower() == 'occurrence':
                occurrence_table = table
                break
        
        if not occurrence_table:
            return Response(
                {'error': 'No occurrence table found'}, 
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Get DataFrame
        df = occurrence_table.df
        if df is None or df.empty:
            return Response({'occurrences': []})
        
        # Match tip labels to scientific names
        from api.helpers.publish import match_tip_label_to_scientific_name
        
        # Get scientific names that match any of the tip labels
        matching_rows = []
        standardized_columns = {str(col).lower(): col for col in df.columns}
        sci_name_col = standardized_columns.get('scientificname', standardized_columns.get('scientific_name'))
        
        if sci_name_col is None:
            return Response({'occurrences': []})
        
        for idx, row in df.iterrows():
            scientific_name = str(row.get(sci_name_col, '')).strip()
            if not scientific_name or scientific_name == 'nan':
                continue
            
            # Check if this scientific name matches any tip label
            for tip_label in tip_labels:
                if match_tip_label_to_scientific_name(tip_label, scientific_name):
                    # Get coordinates if available
                    lat_col = standardized_columns.get('decimallatitude', standardized_columns.get('decimal_latitude', standardized_columns.get('lat')))
                    lon_col = standardized_columns.get('decimallongitude', standardized_columns.get('decimal_longitude', standardized_columns.get('lon')))
                    
                    lat = None
                    lon = None
                    if lat_col and lon_col:
                        try:
                            lat_val = row.get(lat_col)
                            lon_val = row.get(lon_col)
                            if lat_val is not None and lon_val is not None:
                                lat = float(lat_val) if lat_val != '' else None
                                lon = float(lon_val) if lon_val != '' else None
                        except (ValueError, TypeError):
                            pass
                    
                    # Get other relevant fields
                    occ = {
                        'scientificName': scientific_name,
                        'decimalLatitude': lat,
                        'decimalLongitude': lon,
                        'tipLabel': tip_label,
                    }
                    
                    # Add other common fields if available
                    for field in ['occurrenceID', 'catalogNumber', 'recordNumber']:
                        col = standardized_columns.get(field.lower().replace('_', ''))
                        if col and col in row:
                            val = row.get(col)
                            if val is not None and str(val).strip():
                                occ[field] = str(val).strip()
                    
                    matching_rows.append(occ)
                    break  # Found a match, move to next row
        
        return Response({'occurrences': matching_rows})


class TableViewSet(viewsets.ModelViewSet):
    serializer_class = TableSerializer
    permission_classes = [IsAuthenticatedOrSuperuser]
    filterset_fields = ['dataset', 'title']
    ordering = ['-updated_at']

    def get_queryset(self):
        """Filter tables to only show those belonging to the authenticated user's datasets, unless user is superuser"""
        # Superusers can see all tables
        if self.request.user.is_superuser:
            return Table.objects.all().order_by('-updated_at', '-id')
        
        # Regular users only see their own tables
        return Table.objects.filter(dataset__user=self.request.user).order_by('-updated_at', '-id')


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
                for user_file in new_files:
                    description = user_file.filename
                    if user_file.file_type == UserFile.FileType.TREE:
                        preview = self._read_tree_preview(user_file)
                        if preview:
                            description = f"{description} (preview: {preview})"
                    file_descriptions.append(description)
                note_sections.append(f"User uploaded file(s): {', '.join(file_descriptions)}")

            if new_tables:
                note_sections.append("New table ids: [" + ", ".join(str(table.id) for table in new_tables) + "]")

            if note_sections:
                note = "[NOTE: " + "; ".join(note_sections) + "]"
                content = (openai_obj.get('content') or '').strip()
                openai_obj['content'] = f"{content}\n\n{note}" if content else note
                openai_obj['role'] = Message.Role.USER
                serializer.validated_data['openai_obj'] = openai_obj

        serializer.save()

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
