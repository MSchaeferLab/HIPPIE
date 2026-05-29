# HIPPIE_FACELIFT

Clone the repository:

```bash
git clone https://github.com/PelzKo/HIPPIE_FACELIFT.git
cd HIPPIE_FACELIFT
```

Create the virtual environment and install the dependencies:
Because of version conflicts on the server, we are running this with python 3.11 and numpy 1.25

```bash
python3.11 -m venv venv
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

# If you want real data from Intact and BioGrid
cd data
sh download_update_data.sh
cd ..
# Versions change, check what version BIOGRID extracts into
python manage.py hippie_update \
    --biogrid data/BIOGRID-ALL-5.0.257.mitab.txt \
    --intact data/human.txt
python manage.py load_experiment_types --csv_path data/techniques_scoring_04-05-26.csv
python manage.py hippie_update --rescore-all
python manage.py update_tissue_data \
    --gct-path              data/GTEx_Analysis_*_gene_reads.gct \
    --annotation-sample-path data/GTEx_Analysis_*_SampleAttributesDS.txt \
    --entrez-homo-path      data/Homo_sapiens.gene_info
```

# If you want to import the real current data
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

### Loading real data in Docker

`hippie_django/data/` and `hippie_django/logs/` are bind-mounted into the `web`
container, so files downloaded on the host are immediately visible inside and log
files written inside are visible on the host.

```bash
# 1. Download reference files onto the host (into hippie_django/data/)
mkdir -p hippie_django/data hippie_django/logs
cd hippie_django && bash data/download_update_data.sh && cd ..

# — or download inside the running container —
docker compose exec web bash data/download_update_data.sh

# 2. Run the update (paths are relative to the container's WORKDIR)
# Versions change, check what version BIOGRID extracts into
docker compose exec web python manage.py hippie_update \
    --biogrid data/BIOGRID-ALL-5.0.257.mitab.txt \
    --intact  data/human.txt

# 3. Load experiment scoring table, then rescore
docker compose exec web python manage.py load_experiment_types \
    --csv_path data/techniques_scoring_04-05-26.csv
docker compose exec web python manage.py hippie_update --rescore-all

# 4. Load tissue information
# Versions change, check what version is downloaded
docker compose exec web python manage.py update_tissue_data \
    --gct-path               data/GTEx_Analysis_2025-08-22_v11_RNASeQCv2.4.3_gene_reads.gct \
    --annotation-sample-path data/GTEx_Analysis_v11_Annotations_SampleAttributesDS.txt \
    --entrez-homo-path       data/Homo_sapiens.gene_info
```

Migrations and `collectstatic` run automatically on each `web` boot. The
`worker` container reuses the same image with `RUN_MIGRATIONS=0` and
`RUN_COLLECTSTATIC=0` to avoid racing the web container. The `apache`
container enables `proxy`, `proxy_http`, `headers`, `rewrite`, and
`expires` modules and forwards everything except `/static/` and `/media/`
to `web:8000`.
