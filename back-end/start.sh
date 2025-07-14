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

# Load tasks from fixtures (removes existing tasks first)
echo "Loading tasks from fixtures..."
python manage.py load_tasks

# Create superuser if environment variables are set
if [ -n "$DJANGO_SUPERUSER_EMAIL" ] && [ -n "$DJANGO_SUPERUSER_PASSWORD" ]; then
    echo "Creating superuser..."
    python manage.py createsuperuser --noinput --email $DJANGO_SUPERUSER_EMAIL || echo "Superuser already exists or creation failed"
fi

# Collect static files (if needed)
echo "Collecting static files..."
python manage.py collectstatic --noinput

# Start the application
echo "Starting Django server..."
exec "$@" 