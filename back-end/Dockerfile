FROM python:3.12-slim
ENV PYTHONUNBUFFERED 1
WORKDIR /app
COPY ./requirements.txt /srv/requirements.txt
RUN pip install -r /srv/requirements.txt

COPY . /app

# Change the CMD to use Gunicorn with workers and timeout
CMD ["gunicorn", "--workers", "3", "--bind", "0.0.0.0:8000", "--timeout", "180", "app.wsgi:application"]
