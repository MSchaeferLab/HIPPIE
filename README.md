# HIPPIE_FACELIFT

## Setup software

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
Install [Redis](https://redis.io/docs/latest/operate/oss_and_stack/install/archive/install-redis/)

## Setup database

First migrate, then run create superuser and generate the frontend files

```bash
cd hippie_django
python manage.py migrate
python manage.py createsuperuser
npm run build
python manage.py collectstatic

```


## Import data

Use the download script to download the relevant data and import it into HIPPIE

```bash
cd data
sh download_update_data.sh
cd ..
# Versions change, check what version BIOGRID extracts into
python manage.py hippie_update \
    --biogrid data/BIOGRID-ALL-5.0.259.mitab.txt \
    --intact data/human.txt
python manage.py load_experiment_types --csv_path data/user_downloads/techniques_scoring_3.0.tsv
python manage.py update_homology_data \
    --homology_file data/ORTHOLOGY-ALLIANCE_COMBINED_13.tsv \
    --ncbi_gene_info_file data/Homo_sapiens.gene_info \
    --intact_file data/intact.txt

# Scoring just the positive interactions
python manage.py hippie_update --rescore-all

# Import bait-prey association and negative interaction (non-interaction) data
# Requires data/POD_flat.pq — place the file in hippie_django/data/ before running
python manage.py import_pod_data --file data/POD_flat.pq

python manage.py update_tissue_data \
    --gct-path              data/GTEx_Analysis_*_gene_reads.gct \
    --annotation-sample-path data/GTEx_Analysis_*_SampleAttributesDS.txt \
    --entrez-homo-path      data/Homo_sapiens.gene_info

# Refresh Protein.is_reviewed from UniProt's reviewed (Swiss-Prot) accession list
python manage.py update_review_status
```

## Running the server

Start the server
```bash
python manage.py runserver
```
Start the celery worker in a separate terminal to use the ml-split parts
```bash
cd /your/path/to/hippie/HIPPIE_FACELIFT/hippie_django
celery -A hippie worker -l info 2>&1 > celery.log &
```

When you change anything in the frontend, you need to run the following command to build the frontend:

```bash
npm run build
python manage.py collectstatic
```

## Run with Docker Compose

The stack (Redis + Django/Gunicorn + Celery worker + Apache) is defined in
`docker-compose.yml`. The reverse proxy runs `Apache/2.4.66 (Debian)`
(debian:trixie-slim) with `mod_proxy_http` in front of gunicorn (3 workers,
2 threads, 120 s timeout). The Vite React frontend is built inside the web
image via a multi-stage Dockerfile, so no host Node toolchain is required.

**The database is not containerised.** The app connects to a MariaDB running on
the **host machine** via `DB_HOST=host.docker.internal`, which resolves to the
`hippie_net` bridge gateway (`172.18.0.1`). Before `up`, ensure:

- MariaDB is running on the host with the database + user/password from `.env`.
- It listens on all interfaces (`bind-address = 0.0.0.0`, not just `127.0.0.1`).
- The app user is granted from the container network:
  ```sql
  CREATE USER 'hippie'@'%' IDENTIFIED BY 'password';
  GRANT ALL ON hippie.* TO 'hippie'@'%';
  ```
  (Connections arrive via `172.18.0.1`, not loopback.)

```bash
cp .env.example .env       # then edit secrets / passwords / domain
docker compose build
docker compose up -d

# Migrations are manual (RUN_MIGRATIONS=0) — run once the DB is reachable:
docker compose exec web python manage.py migrate
```

Open `http://localhost:8080/`.

**Sub-path deployment** (e.g. `https://example.com/hippie/`): set
`APACHE_PUBLISHED_PATH=/hippie` in `.env`
before `up`. Apache mounts `/static/` and `/media/` under that prefix via
`Alias` directives; Django uses `DJANGO_SCRIPT_NAME` to build correct URLs.
Override `DJANGO_STATIC_URL` only if your static path differs from the default.
If it is not set, the app will use `APACHE_PUBLISHED_PATH` as `DJANGO_STATIC_URL`

For production domains, also set in `.env`:

```
DJANGO_ALLOWED_HOSTS=example.com
DJANGO_CSRF_TRUSTED_ORIGINS=https://example.com
```

Add the following block to your host Apache config (`/etc/apache2/apache2.conf`
or a site conf in `/etc/apache2/sites-enabled/`) to proxy the containerised
stack:

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
docker compose down -v           # stop + wipe static / media volumes (host DB untouched)
```

### Loading real data in Docker

`hippie_django/data/` and `hippie_django/logs/` are bind-mounted into the `web`
container, so files downloaded on the host are immediately visible inside and log
files written inside are visible on the host.

Public release files (generated by `python manage.py export_downloads`) live in
`hippie_django/data/user_downloads/`. Apache serves them directly at
`/downloads/<file>` (bind-mounted read-only to `/vol/downloads`), bypassing
Django/gunicorn — see `docker/apache/hippie.conf.template`. In dev (`runserver`,
no Apache) the same `/downloads/<file>` URL is served by the `download_dataset`
view as a fallback.

```bash
# 1. Download reference files onto the host (into hippie_django/data/)
mkdir -p hippie_django/data hippie_django/logs
cd hippie_django && bash data/download_update_data.sh && cd ..

# — or download inside the running container —
docker compose exec web bash data/download_update_data.sh

# 2. Run the update (paths are relative to the container's WORKDIR)
# Versions change, check what version BIOGRID extracts into
docker compose exec web python manage.py hippie_update \
    --biogrid data/BIOGRID-ALL-5.0.259.mitab.txt \
    --intact  data/human.txt

# 3. Load experiment scoring table
docker compose exec web python manage.py load_experiment_types \
    --csv_path data/user_downloads/techniques_scoring_3.0.tsv

# 4. Load homology / orthology data
docker compose exec web python manage.py update_homology_data \
    --homology_file      data/ORTHOLOGY-ALLIANCE_COMBINED_13.tsv \
    --ncbi_gene_info_file data/Homo_sapiens.gene_info \
    --intact_file        data/intact.txt

# 5. Rescoring
docker compose exec web python manage.py hippie_update --rescore-all

# 6. Import bait-prey association and negative interaction (non-interaction) data
# Requires data/POD_flat.pq — place the file in hippie_django/data/ before running
docker compose exec web python manage.py import_pod_data --file data/POD_flat.pq

# 7. Load tissue information
# Versions change, check what version is downloaded
docker compose exec web python manage.py update_tissue_data \
    --gct-path               data/GTEx_Analysis_2025-08-22_v11_RNASeQCv2.4.3_gene_reads.gct \
    --annotation-sample-path data/GTEx_Analysis_v11_Annotations_SampleAttributesDS.txt \
    --entrez-homo-path       data/Homo_sapiens.gene_info

# 8. Refresh Protein.is_reviewed from UniProt's reviewed (Swiss-Prot) accession list
docker compose exec web python manage.py update_review_status
```

`collectstatic` runs automatically on each `web` boot; migrations are manual
(`RUN_MIGRATIONS=0`) so they are never applied automatically against the host
DB — run `docker compose exec web python manage.py migrate` deliberately. The
`worker` container reuses the same image with `RUN_MIGRATIONS=0` and
`RUN_COLLECTSTATIC=0` to avoid racing the web container. The `apache`
container enables `proxy`, `proxy_http`, `headers`, `rewrite`, and
`expires` modules and forwards everything except `/static/` and `/media/`
to `web:8000`.
