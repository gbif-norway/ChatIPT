# ORCID OAuth2 Setup for ChatIPT

This guide explains how to set up ORCID OAuth2 authentication for the ChatIPT application with a React frontend.

## Architecture Overview

- **Backend**: Django API with ORCID OAuth2 authentication
- **Frontend**: React application that communicates with the Django API
- **Authentication Flow**: ORCID OAuth2 → Django backend → Session-based API access

## Prerequisites

1. An ORCID account (register at https://orcid.org/register)
2. Access to ORCID's developer tools
3. React frontend application

## Step 1: Register Your Application with ORCID

1. Go to https://orcid.org/developer-tools
2. Sign in with your ORCID account
3. Click "Register for the free ORCID public API"
4. Fill in the application details:
   - **Application name**: ChatIPT
   - **Application website**: Your domain (e.g., https://chatipt.svc.gbif.no)
   - **Application description**: ChatIPT is a chatbot for students and researchers who are new to data publication
   - **Redirect URIs**: 
     - For development: `http://localhost:8000/api/auth/orcid/callback/`
     - For production: `https://yourdomain.com/api/auth/orcid/callback/`
5. Save the application

**Important**: ChatIPT now requests the `openid`, `/authenticate`, and `/read-limited` scopes.  
- `/read-limited` requires ORCID member (or sandbox member) API credentials.  
- Without that permission, ORCID will reject the login request. Remove the scope from `SOCIALACCOUNT_PROVIDERS["orcid"]["SCOPE"]` if you only have public API access.

## Step 2: Get Your Credentials

After registering, you'll receive:
- **Client ID**: A public identifier for your application
- **Client Secret**: A private key for your application

## Step 3: Configure Environment Variables

### Option A: Using Docker Compose (Recommended)

1. Copy the example environment file:
   ```bash
   cp back-end/env.example back-end/.env.dev
   ```

2. Edit `back-end/.env.dev` with your actual values:
   ```bash
   # Django Settings
   DJANGO_SECRET_KEY=your-actual-secret-key
   DEBUG=True
   ALLOWED_HOSTS=localhost,127.0.0.1

   # Database Settings
   SQL_ENGINE=django.db.backends.postgresql
   SQL_DATABASE=chatipt
   SQL_USER=postgres
   SQL_PASSWORD=password
   SQL_HOST=db
   SQL_PORT=5432

   # ORCID OAuth2 Settings
   ORCID_CLIENT_ID=your-actual-orcid-client-id
   ORCID_CLIENT_SECRET=your-actual-orcid-client-secret

   # Optional: Create superuser on startup
   DJANGO_SUPERUSER_EMAIL=admin@yourdomain.com
   DJANGO_SUPERUSER_PASSWORD=your-superuser-password
   ```

### Option B: Direct Environment Variables

Set these environment variables in your deployment:

```bash
# ORCID OAuth2 credentials
ORCID_CLIENT_ID=your_client_id_here
ORCID_CLIENT_SECRET=your_client_secret_here

# Django settings
DJANGO_SECRET_KEY=your_django_secret_key
DEBUG=False  # Set to True for development
ALLOWED_HOSTS=yourdomain.com,www.yourdomain.com
```

## Step 4: Automated Startup (Docker)

The backend container now automatically handles setup on startup:

1. **Database Migration**: Runs `python manage.py migrate`
2. **ORCID Setup**: Runs `python manage.py setup_orcid`
3. **Superuser Creation**: Creates superuser if environment variables are set
4. **Static Files**: Collects static files
5. **Server Start**: Starts the Django server

### Using Docker Compose

```bash
# Start all services
docker-compose up

# Or start just the backend
docker-compose up back-end
```

### Manual Setup (if needed)

If you need to run setup manually:

```bash
# Run migrations
python manage.py migrate

# Set up ORCID provider
python manage.py setup_orcid

# Create superuser (optional)
python manage.py createsuperuser
```

## Step 5: Configure CORS for React Frontend

Update your Django settings to allow your React frontend domain:

```python
CORS_ALLOWED_ORIGINS = [
    "http://localhost:3000",  # React dev server
    "https://yourdomain.com",  # Production frontend
]

CORS_ALLOW_CREDENTIALS = True
```

## API Endpoints

### Authentication Endpoints

- `GET /api/auth/status/` - Check authentication status (public)
- `GET /api/auth/profile/` - Get user profile (authenticated)

### ORCID OAuth Endpoints

- `GET /accounts/orcid/login/` - Start ORCID OAuth flow
- `GET /accounts/orcid/login/callback/` - ORCID OAuth callback
- `GET /accounts/logout/` - Logout

### Protected API Endpoints

All existing API endpoints now require authentication:
- `GET /api/datasets/` - User's datasets only
- `POST /api/datasets/` - Create dataset (auto-assigns user)
- `GET /api/tables/` - User's tables only
- `GET /api/messages/` - User's messages only
- `GET /api/agents/` - User's agents only

## React Frontend Integration

### Authentication Flow

1. **Check Auth Status**: Call `GET /api/auth/status/` on app load
2. **Redirect to Login**: If not authenticated, redirect to `/accounts/orcid/login/`
3. **Handle Callback**: ORCID redirects back to Django, which sets session
4. **Get User Info**: Call `GET /api/auth/profile/` to get user details
5. **API Calls**: Include credentials in all subsequent API calls

### Example React Implementation

```javascript
// Check authentication status
const checkAuth = async () => {
  const response = await fetch('/api/auth/status/', {
    credentials: 'include'
  });
  const data = await response.json();
  
  if (data.authenticated) {
    setUser(data.user);
    setAuthenticated(true);
  } else {
    // Redirect to ORCID login
    window.location.href = '/accounts/orcid/login/';
  }
};

// Make authenticated API calls
const fetchDatasets = async () => {
  const response = await fetch('/api/datasets/', {
    credentials: 'include'
  });
  return response.json();
};
```

### Session Management

The Django backend uses session-based authentication. Ensure your React app:
- Includes `credentials: 'include'` in fetch requests
- Handles session expiration gracefully
- Redirects to login when receiving 401 responses

## How It Works

1. **User Authentication**: React redirects to Django's ORCID login endpoint
2. **ORCID Authorization**: User authorizes ChatIPT on ORCID
3. **Session Creation**: Django creates a session and redirects back to React
4. **API Access**: React makes authenticated API calls using session cookies
5. **Data Isolation**: All API responses are filtered to user's data only

## User Model

The `CustomUser` model extends Django's `AbstractUser` and includes:

- `orcid_id`: The user's ORCID identifier
- `orcid_access_token`: OAuth access token for API calls
- `orcid_refresh_token`: OAuth refresh token
- `institution`: User's institution (from ORCID profile)
- `department`: User's department
- `country`: User's country

## Security Considerations

1. **HTTPS Required**: Always use HTTPS in production
2. **CORS Configuration**: Properly configure CORS for your frontend domain
3. **Session Security**: Sessions are secure and tied to user accounts
4. **User Isolation**: Users can only access their own datasets
5. **ORCID Verification**: Users are verified through ORCID's authentication system

## Troubleshooting

### Common Issues

1. **CORS Errors**: Ensure CORS is properly configured for your frontend domain
2. **Session Not Persisting**: Check that `credentials: 'include'` is used in fetch requests
3. **"Invalid redirect URI"**: Ensure the redirect URI in your ORCID app matches exactly
4. **"Client ID not found"**: Check that your environment variables are set correctly
5. **Startup Failures**: Check container logs for migration or setup errors
6. **Public profile fetch failed**: The backend now falls back to `/userinfo` data, but you'll see a warning if ORCID returns 403/404. Confirm that your ORCID client is approved for `/read-limited` or that the user has made some data public.

### Debug Mode

For development, you can enable debug mode:

```bash
DEBUG=True
```

This will show detailed error messages and disable HTTPS requirements.

### Container Logs

Check container logs for startup issues:

```bash
# View backend logs
docker-compose logs back-end

# Follow logs in real-time
docker-compose logs -f back-end
```

## Production Deployment

For production deployment:

1. Set `DEBUG=False`
2. Use a proper database (PostgreSQL recommended)
3. Configure HTTPS
4. Set secure `DJANGO_SECRET_KEY`
5. Update `ALLOWED_HOSTS` and `CORS_ALLOWED_ORIGINS` with your domains
6. Configure proper logging
7. Set up static file serving

## API Access

The REST API now requires authentication. All endpoints are protected and will only return data belonging to the authenticated user.

## Support

For issues with ORCID integration, check:
- ORCID Developer Documentation: https://members.orcid.org/api
- Django Allauth Documentation: https://django-allauth.readthedocs.io/
- ChatIPT Issues: [Your repository issues page] 