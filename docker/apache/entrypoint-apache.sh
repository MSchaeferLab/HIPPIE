#!/usr/bin/env sh
set -eu

# Substitute APACHE_PUBLISHED_PATH (may be empty for root deployment).
# Only this var is expanded — Apache's own ${APACHE_LOG_DIR} is left
# intact so apache2ctl can resolve it via /etc/apache2/envvars.
: "${APACHE_PUBLISHED_PATH:=}"
export APACHE_PUBLISHED_PATH

envsubst '${APACHE_PUBLISHED_PATH}' \
    < /etc/apache2/sites-available/hippie.conf.template \
    > /etc/apache2/sites-available/hippie.conf

a2ensite hippie >/dev/null

exec "$@"
