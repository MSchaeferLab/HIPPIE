#!/usr/bin/env bash
set -euo pipefail

DB_HOST="${DB_HOST:-db}"
DB_PORT="${DB_PORT:-3306}"

# Wait for the database to accept TCP connections.
echo "Waiting for ${DB_HOST}:${DB_PORT}..."
until nc -z "${DB_HOST}" "${DB_PORT}"; do
    sleep 1
done
echo "Database reachable."

if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
    echo "Applying migrations..."
    python manage.py migrate --noinput
fi

if [ "${RUN_COLLECTSTATIC:-1}" = "1" ]; then
    echo "Collecting static files..."
    python manage.py collectstatic --noinput
fi

exec "$@"
