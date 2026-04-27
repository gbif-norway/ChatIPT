#!/bin/bash

# Exit on any error
set -e

echo "Starting ChatIPT backend..."

# Wait for database to be ready (if using external database)
if [ -n "$SQL_HOST" ]; then
    echo "Waiting for database at $SQL_HOST:$SQL_PORT..."
    while ! nc -z $SQL_HOST $SQL_PORT; do
        sleep 1
    done
    echo "Database is ready!"
fi

# Run database migrations
echo "Running database migrations..."
python manage.py migrate

# Set up ORCID provider
echo "Setting up ORCID provider..."
python manage.py setup_orcid

# Load tasks from fixtures (upserts by task name; does not delete existing rows)
echo "Loading tasks from fixtures..."
python manage.py load_tasks

# Create superuser when email/password are provided.
# Keep username aligned with email to match project auth conventions.
if [ -n "$DJANGO_SUPERUSER_EMAIL" ] && [ -n "$DJANGO_SUPERUSER_PASSWORD" ]; then
    echo "Creating superuser..."
    python manage.py createsuperuser --noinput --username "$DJANGO_SUPERUSER_EMAIL" --email "$DJANGO_SUPERUSER_EMAIL" || echo "Superuser already exists or creation failed"
elif [ -n "$DJANGO_SUPERUSER_EMAIL" ] || [ -n "$DJANGO_SUPERUSER_PASSWORD" ]; then
    echo "Skipping superuser creation (set both DJANGO_SUPERUSER_EMAIL and DJANGO_SUPERUSER_PASSWORD to enable it)."
fi

# Collect static files unless explicitly skipped (useful for local dev)
if [ "${SKIP_COLLECTSTATIC:-0}" = "1" ]; then
    echo "Skipping static collection (SKIP_COLLECTSTATIC=1)."
else
    echo "Collecting static files..."
    python manage.py collectstatic --noinput
fi

# Start the application
echo "Starting Django server..."
exec "$@" 
