# syntax=docker/dockerfile:1.7

# ── stage 1: build the Vite React bundles ────────────────────────────────────
FROM node:20-alpine AS frontend
WORKDIR /app

ARG APACHE_PUBLISHED_PATH=""
ENV APACHE_PUBLISHED_PATH=$APACHE_PUBLISHED_PATH
ARG DJANGO_STATIC_URL=""
ENV DJANGO_STATIC_URL=$DJANGO_STATIC_URL

COPY package*.json ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

COPY frontend ./frontend
COPY vite.config.js ./

RUN npm run build

# ── stage 2: Django runtime ──────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        pkg-config \
        default-libmysqlclient-dev \
        netcat-openbsd \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /code

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY hippie_django ./hippie_django

# Copy the built frontend bundles into the Django static dir.
COPY --from=frontend /app/hippie_django/hippie_website/static/hippie_website/js \
     ./hippie_django/hippie_website/static/hippie_website/js

COPY docker/web/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /code/hippie_django

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
CMD ["gunicorn", "hippie.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "3", \
     "--threads", "2", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
