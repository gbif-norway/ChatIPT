FROM python:3.12-slim
ENV PYTHONUNBUFFERED 1
WORKDIR /app

# Install system dependencies including netcat for database connectivity checks
RUN apt-get update && apt-get install -y \
    netcat-traditional \
    && rm -rf /var/lib/apt/lists/*

COPY ./requirements.txt /srv/requirements.txt
RUN pip install -r /srv/requirements.txt

COPY . /app

# Make the startup script executable
RUN chmod +x /app/start.sh

# Use the startup script as the entrypoint
ENTRYPOINT ["/app/start.sh"]
CMD ["gunicorn", "--workers", "3", "--bind", "0.0.0.0:8000", "--timeout", "3600", "app.wsgi:application"]
