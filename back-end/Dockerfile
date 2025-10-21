FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install system dependencies including netcat for database connectivity checks
RUN apt-get update && apt-get install -y \
    netcat-traditional \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Copy requirements first for better layer caching
COPY requirements.txt /srv/requirements.txt

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /srv/requirements.txt

# Copy application code
COPY . /app

# Make the startup script executable
RUN chmod +x /app/start.sh

# Use the startup script as the entrypoint
ENTRYPOINT ["/app/start.sh"]
CMD ["gunicorn", "--workers", "3", "--bind", "0.0.0.0:8000", "--timeout", "3600", "app.wsgi:application"]
