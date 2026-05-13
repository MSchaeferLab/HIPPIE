# HIPPIE_FACELIFT

Clone the repository:

```bash
git clone https://github.com/PelzKo/HIPPIE_FACELIFT.git
cd HIPPIE_FACELIFT
```

Create the virtual environment and install the dependencies:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

First migrate, run create superuser and then run the server:

```bash
cd hippie_django
python manage.py migrate

# If you want example data
python manage.py seed_test_data
python manage.py test_import_bait_prey
# If you want to import the real data
python manage.py import_hippie_sql data/mschaefer_hippie_v2_v2-4.sql --log-file data/import.log

python manage.py createsuperuser
npm run build
```

Start the celery working in a seperate terminal
```bash
cd hippie_django
celery -A hippie worker -l info 2>&1 > celery.log &
```
Start the server in the first terminal
```bash
python manage.py runserver
```

When you change anything in the frontend, you need to run the following command to build the frontend:

```bash
npm run build
python manage.py collectstatic
```

## Run with Docker Compose

A full stack (MariaDB + Redis + Django/Gunicorn + Celery worker + Apache) is
defined in `docker-compose.yml`. The reverse proxy runs `Apache/2.4.66
(Debian)` (debian:trixie-slim base) with `mod_proxy_http` in front of
gunicorn. The Vite React frontend is built inside the web image via a
multi-stage Dockerfile, so no host Node toolchain is required.

```bash
cp .env.example .env       # then edit secrets / passwords
docker compose build
docker compose up -d
```

Open `http://localhost:8080/`. To deploy under a sub-path, set
`APACHE_PUBLISHED_PATH=/hippie` in `.env` before `up`; Apache will
mount `/static/` and `/media/` under that prefix via `Alias` directives.

Add the following block to your /etc/apache2/apache2.conf:

```apache
# HIPPIE Django app
RedirectMatch ^/hippie$ /hippie/
ProxyPreserveHost On
ProxyPass        /hippie/  http://localhost:8080/hippie/
ProxyPassReverse /hippie/  http://localhost:8080/hippie/
RequestHeader    set X-Forwarded-Proto "https"
RequestHeader    set X-Forwarded-Port  "443"
```

Useful one-shots:

```bash
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py seed_test_data
docker compose exec web python manage.py test_import_bait_prey
docker compose logs -f web worker apache
docker compose down              # stop; volumes preserved
docker compose down -v           # stop + wipe DB / static / media volumes
```

Migrations and `collectstatic` run automatically on each `web` boot. The
`worker` container reuses the same image with `RUN_MIGRATIONS=0` and
`RUN_COLLECTSTATIC=0` to avoid racing the web container. The `apache`
container enables `proxy`, `proxy_http`, `headers`, `rewrite`, and
`expires` modules and forwards everything except `/static/` and `/media/`
to `web:8000`.
