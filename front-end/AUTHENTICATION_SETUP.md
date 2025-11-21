# ChatIPT Frontend Authentication Setup

This document explains how the ORCID OAuth2 authentication is implemented in the ChatIPT frontend.

## Overview

The frontend now requires ORCID authentication before users can access the ChatIPT application. The authentication flow is:

1. User visits the application
2. If not authenticated, they see a login page
3. User clicks "Sign in with ORCID" 
4. Frontend redirects to backend OAuth2 endpoint
5. Backend redirects to ORCID for authentication
6. ORCID redirects back to backend callback endpoint
7. Backend creates/updates user and logs them in
8. Backend redirects back to frontend
9. User can now access ChatIPT with their datasets linked to their ORCID account

## Environment Configuration

Create a `.env.local` file in the frontend directory with:

```bash
# Backend base URL (clean, without /api/ suffix)
NEXT_PUBLIC_BASE_API_URL=http://localhost:8000
```

For production, update this to your backend URL. **Important**: Use the clean base URL without `/api/` - the full API paths are specified in each request.

## Components

### AuthContext (`app/contexts/AuthContext.js`)
- Manages authentication state throughout the application
- Provides login/logout functions
- Checks authentication status on app load

### Login (`app/components/Login.js`)
- Displays the login page when user is not authenticated
- Shows information about ORCID authentication
- Provides the "Sign in with ORCID" button

### Header (`app/components/Header.js`)
- Shows user information when authenticated
- Displays user's name, email, ORCID ID, and institution
- Provides logout functionality

### ProtectedRoute (`app/components/ProtectedRoute.js`)
- Wraps protected content
- Redirects to login if not authenticated
- Shows loading state while checking authentication

## API Integration

All API calls now include credentials for session-based authentication:

```javascript
const fetchData = async (url, options = {}) => {
  const response = await fetch(url, {
    ...options,
    credentials: 'include' // Include credentials for authenticated requests
  });
  if (!response.ok) throw new Error('Network response was not ok');
  return response.json();
};
```

## Backend Requirements

The backend must be configured with:

1. ORCID OAuth2 credentials
2. CORS settings to allow the frontend domain
3. Session-based authentication
4. Proper redirect URLs
5. At least one public field on each researcher's ORCID profile (fully private records are redirected with `error=public_profile_required`)

## Development Setup

1. Start the backend server (Django with ORCID OAuth2)
2. Set the `NEXT_PUBLIC_BASE_API_URL` environment variable
3. Start the frontend development server: `npm run dev`
4. Visit `http://localhost:3000`
5. You'll be redirected to login if not authenticated

## Production Deployment

For production:

1. Update `NEXT_PUBLIC_BASE_API_URL` to your production backend URL
2. Update backend CORS settings to include your production frontend domain
3. Update backend redirect URLs to point to your production frontend
4. Ensure HTTPS is used for both frontend and backend

## User Experience

- Users must have an ORCID account to use ChatIPT
- No separate signup process - ORCID authentication is sufficient
- User's datasets are automatically linked to their ORCID account
- User information (name, institution, etc.) is pulled from ORCID profile
- Users can see their ORCID ID and institution in the header
- Users can logout and login with different ORCID accounts

## Security

- Session-based authentication with secure cookies
- CORS properly configured to prevent unauthorized access
- All API endpoints require authentication
- User data is isolated - users can only access their own datasets
- ORCID provides verified academic identity 