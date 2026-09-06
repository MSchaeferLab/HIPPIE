# HIPPIE_FACELIFT

## Setup software

Clone the repository:

```bash
git clone --recurse-submodules https://github.com/PelzKo/HIPPIE_FACELIFT.git
cd HIPPIE_FACELIFT
```

`--recurse-submodules` fetches `pipeline/`, the pinned checkout of
[ppi-splitting-pipeline](https://github.com/PelzKo/ppi-splitting-pipeline) that
generates the ML splits. In an existing clone:

```bash
git submodule update --init
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
mkdir -p secrets && cp /path/to/gurobi.lic secrets/   # see "ML splits" below
docker compose build
docker compose up -d
```

`docker compose build` builds two images: `docker/web/Dockerfile` for `web` and
`mcp`, and `docker/worker/Dockerfile` for `worker`. The worker image is ~2 GB
larger because it additionally carries Nextflow, a JRE and the ML-splits solver
toolchain — see [ML splits](#ml-splits-nextflow) below.

Migrations apply automatically, and the stack is sequenced around them: only
`web` has `RUN_MIGRATIONS=1`, it migrates in its entrypoint before gunicorn
starts, and `worker`, `mcp` and `apache` all wait for `web` to report healthy
(`GET /status/` — database, cache, and no pending migration) before they start.
So nothing serves traffic or touches Celery until the schema is current, and no
two containers can race `migrate` (MariaDB has no transactional DDL).

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

# MCP endpoint first: ProxyPass rules match in order, and the catch-all below
# would otherwise swallow this one. The long timeout and the two SetEnvs are
# what keep MCP's streamable HTTP working — it holds the response open, so the
# default 60s ProxyTimeout would cut long calls off, and a buffering output
# filter (gzip) would defeat the streaming.
ProxyPass        /hippie/mcp  http://localhost:8080/hippie/mcp timeout=300
ProxyPassReverse /hippie/mcp  http://localhost:8080/hippie/mcp
<Location /hippie/mcp>
    SetEnv proxy-sendchunked 1
    SetEnv no-gzip 1
</Location>

ProxyPass        /hippie/  http://localhost:8080/hippie/
ProxyPassReverse /hippie/  http://localhost:8080/hippie/

# TLS terminates here, so this hop states the real scheme. The containerised
# Apache uses `setifempty` for the same header and therefore preserves it;
# Django trusts it via SECURE_PROXY_SSL_HEADER, which is what makes
# request.scheme (and every absolute URL built from it) say https.
RequestHeader    set X-Forwarded-Proto "https"
RequestHeader    set X-Forwarded-Port  "443"
```

Both this hop and the containerised one *append* to `X-Forwarded-For`, so the
MCP rate limiter reads the client IP two entries from the right
(`HIPPIE_MCP_TRUSTED_PROXY_HOPS=2`, the default). Adding or removing a proxy in
front of this stack means changing that number.

Useful one-shots:

```bash
docker compose exec web python manage.py createsuperuser
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

# 9. Regenerate the public download files (see below)
docker compose exec web python manage.py export_downloads data/user_downloads
```

### ML splits (Nextflow)

The **Generate ML Splits** page hands the filtered interaction set to the
[ppi-splitting-pipeline](https://github.com/PelzKo/ppi-splitting-pipeline)
running `--split_only`, and returns a zip with four labelled CSVs. Nothing about
the pipeline is user-configurable: every parameter is fixed in
`conf/hippie.config` and the page only chooses *which interactions* go in.

What a run does, in order:

| Step | What it is |
|---|---|
| export | Django streams the filtered interactions to `ppis.csv` (`protein1,protein2,score`) |
| `SORT_PPIS` | canonical edge ordering |
| `SOLVE_ILP` | assigns the 100 precomputed sequence-similarity clusters to train/val/test |
| `CDHIT2D` + `REMOVE_REDUNDANT` | drops train/test pairs that are homologous, so the split is leakage-aware by sequence and not only by graph cut |
| `SAMPLE_NEGATIVES_ILP` | negatives matched on degree, taxon-pair, self-loop and GO-Jaccard bias |

Output: `train.csv`, `val.csv`, `test_balanced.csv` (1:1) and
`test_realistic.csv` (1:10, uniform negatives), plus a generated `summary.json`
and `README.txt` naming the filters and the pipeline commit.

**Runs take tens of minutes to two hours** and concurrency is pinned to 1
(`--concurrency=1` on the Celery worker, `process.resourceLimits = [cpus: 2,
memory: '8.GB', time: '4.h']` in the config, so the pipeline's own much larger
`withName:` requests get clamped rather than deadlocking). `SOLVE_ILP` alone
spends its full 1800 s budget on essentially any input — the assignment problem
is hard to prove optimal even though it is small — so that cost is a floor, not
something a narrow filter avoids. Each run card has a **Cancel** button, which
is what makes a mis-clicked two-hour run recoverable.

#### Three things must be in place

**1. Precomputed proteome artifacts** — `hippie_django/data/precomputed/`:

```
sequences.fasta            all 29,080 proteins, isoform accessions included
go_annotations.tsv         GO BP/MF/CC per protein
species.tsv                NCBI taxon id per protein
partitioned_proteome.txt   KaHIP partition, 100 clusters
node_mapping.tsv           KaHIP node id -> accession
```

These are **built out of band, once per HIPPIE release** — a full pipeline run
including BLAST (64 cpu / 32 GB) and KaHIP (4 cpu / 8 GB), which cannot happen
inside a request on a 2-cpu box. The web app only ever reads them. Refreshing
means replacing the directory. `all_vs_all.tsv` also lives with them upstream
and is deliberately **not** copied here: it is the BLAST graph KaHIP was run
over, `--split_only` never reads it, and it is ~470 MB.

The directory is inside the existing `./hippie_django/data` bind mount, so no
new volume is needed — but note the `worker` service mounts it **read-only**.

Every interaction whose endpoints are not in `node_mapping.tsv` is dropped
before the run and the count is recorded in `job.summary["interactions_dropped"]`.
For a matching release that count is 0; a non-zero value means the artifacts and
the database have drifted apart.

**2. A Gurobi WLS licence** at `secrets/gurobi.lic` (or wherever
`GUROBI_LICENSE_PATH` points), mounted read-only into the worker at
`/etc/gurobi/gurobi.lic`.

WLS is the only Gurobi licence type that validates **inside a container** — a
Named-User Academic licence is node-locked to a hostid the container does not
have. Academic WLS is free.

- The worker needs outbound HTTPS to `license.gurobi.com` for the check-out.
- `withLabel:gurobi`'s `maxForks = 2` in the pipeline is there for the WLS
  concurrent-session cap and must stay.
- Both solvers are pinned explicitly (`ilp_solver = "GUROBI"`,
  `neg_ilp_solver = "gurobi"`), so there is **no fallback**: a run that finishes
  provably used Gurobi. A licence expiry or a sustained network outage fails the
  run loudly rather than quietly producing a differently-optimised result under
  the same job id. Transient "too many sessions" collisions are already covered
  by the pipeline's retry ladder (3 attempts, 30/60/90 s backoff).
- **Never bake the licence into an image.** `secrets/`, `*.lic` are in both
  `.gitignore` and `.dockerignore`: the WLSACCESSID/WLSSECRET pair is
  recoverable from any layer that has ever held it.

**3. The pipeline checkout** at `pipeline/` (a git submodule), mounted
read-only at `/opt/pipeline`. Set `PIPELINE_PATH` in `.env` to point at a
working tree elsewhere during development. Because `bin/` is mounted rather than
baked, the checkout's commit is a result-determining input to every split, so
each run records `git rev-parse HEAD` in `job.summary["pipeline_commit"]`.

#### Where a run's files live

Per-job directories are under the `nf_work` volume at `/var/nf/<job-uuid>/`
(`ppis.csv`, `samplesheet.csv`, `work/`, `results/`, `.nextflow.log`,
`trace.txt`). This is deliberately **not** under `MEDIA_ROOT`: Apache serves
`/vol/media/` directly, and Nextflow stages a copy of the licence into every
task's work dir.

The directory is deleted on success and on cancel, and **kept on failure** so
`.nextflow.log` and the failing task's `.command.err` are still readable — both
tails are also captured into `job.error` and shown on the run card. Only the zip
at `MEDIA_ROOT/splits/<uuid>.zip` persists. There is no age-based retention.

To debug a failed run:

```bash
docker compose exec worker ls /var/nf
docker compose exec worker tail -50 /var/nf/<job-uuid>/.nextflow.log
docker compose exec worker sh -c 'find /var/nf/<job-uuid>/work -name .command.err -size +0 -exec tail -30 {} +'
```

The toolchain the pipeline's `bin/*.py` run under is a second Python at
`/opt/conda` (3.14.7 + cvxpy + gurobipy + cd-hit), deliberately kept **off** the
global `PATH`. `conf/hippie.config`'s `env` scope puts it there for task shells
only, so Django and Celery keep resolving `python` to the image's 3.11 and the
two dependency sets never have to satisfy each other.

### Updating the public download files

`export_downloads` regenerates the three files served at `/downloads/<file>`.
It reads the database only — it downloads nothing — so run it **after** the
import steps above, whenever interactions, scores, evidence or orthology data
have changed. Anything else leaves the published files describing a stale
database.

```bash
docker compose exec web python manage.py export_downloads data/user_downloads
```

The `path` argument is relative to the container's WORKDIR (`/code/hippie_django`),
so `data/user_downloads` resolves to `hippie_django/data/user_downloads/` on the
host via the bind mount. Files are overwritten in place; the directory is created
if missing. Written:

| File | Contents |
|------|----------|
| `HIPPIE-current.mitab.txt.gz`  | PSI-MI TAB 2.5 — 15 mandatory columns + 3 HIPPIE extensions (`Presence In Other Species`, `Gene Name Interactor A/B`) |
| `HIPPIE-current.txt.gz`        | compact tab format keyed on `uniprot_accession` |
| `HIPPIE-current.stats.txt.gz`  | score quartiles and counts over {interactions, non-interactions, both} × {all, isoforms-only, non-isoforms-only} |

Runtime notes:

* The export streams every `Interaction` in chunks of 10 000 with a server-side
  cursor, so it takes minutes on a full database and holds one connection open
  throughout. Run it in a shell you can leave open (or under `nohup` /
  `tmux`) — a dropped connection mid-run leaves partially written `.gz` files.
* Only `NonInteraction` scores reach the stats file; the two download files
  cover interactions only, because non-interactions carry no evidence.
* Conserved species come from `OrthologInteraction` (keyed on the **gene** pair,
  not the interaction), which the command loads into memory once up front. If
  `Presence In Other Species` is empty everywhere, step 4
  (`update_homology_data`) has not been run against the current data.
* Apache serves `hippie_django/data/user_downloads/` read-only at
  `/downloads/<file>` directly from disk, so new files are live as soon as the
  command finishes — no restart needed.

`collectstatic` and `migrate` both run automatically on each `web` boot, in that
container's entrypoint and before gunicorn starts. `worker` and `mcp` reuse the
same image with `RUN_MIGRATIONS=0` and `RUN_COLLECTSTATIC=0` and wait on `web`'s
health check, so exactly one container migrates and nothing serves against a
stale schema. The `apache`
container enables `proxy`, `proxy_http`, `headers`, `rewrite`, and
`expires` modules and forwards everything except `/static/` and `/media/`
to `web:8000`.
