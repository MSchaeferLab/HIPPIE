# syntax=docker/dockerfile:1.7
#
# Celery worker image: the Django runtime plus everything `nextflow run
# ppi-splitting-pipeline --split_only` needs to execute in-process.
#
# WHY THIS IS A SEPARATE FILE FROM docker/web/Dockerfile
#
# web and mcp both build from docker/web/Dockerfile. The toolchain below (a JRE,
# nextflow, and a second Python with cvxpy/gurobipy/scipy/cd-hit) is ~1.5 GB
# that neither of them can ever execute — it belongs only to the container that
# runs splits. The duplicated Django-runtime block is the price; keep the two in
# step when either changes.
#
# What is deliberately NOT here: the node/Vite stage. The worker serves nothing,
# runs with RUN_COLLECTSTATIC=0, and never reads the frontend bundles.
#
# TWO PYTHONS, ON PURPOSE
#
# Django and Celery run on the image's python 3.11 with requirements.txt. The
# pipeline's bin/*.py run on the micromamba env at /opt/conda (python 3.14.7).
# /opt/conda/bin is NOT on the global PATH — conf/hippie.config puts it there
# through Nextflow's `env` scope, which reaches task shells only. Merging them
# would mean one dependency resolution has to satisfy both Django and cvxpy,
# and a solver bump would then be a website deployment.

# ── stage 1: the pipeline's solver toolchain ─────────────────────────────────
# An exact replica of ppi-splitting-pipeline's docker/Dockerfile.split_only
# package set, built on the same base so the pins resolve identically. Copying
# the finished /opt/conda out of it (rather than bootstrapping micromamba into
# python:3.11-slim by hand) is what keeps those pins the single source of truth
# for the toolchain — a `--split_only` run here and a `-with-docker
# ppi-splitting-split-only` run get the same solver versions, so their ILP
# results are comparable.
#
# The prefix must stay /opt/conda in both stages: a conda env is only
# relocatable to an identical path.
FROM mambaorg/micromamba:2.0.5 AS solver

USER root

# cd-hit 4.8.1 provides cd-hit-2d (CDHIT2D). pyscipopt is present because the
# upstream image carries it; HIPPIE pins ilp_solver/neg_ilp_solver to Gurobi
# explicitly, so it is never selected here.
RUN micromamba install -y -n base -c conda-forge -c bioconda \
      python=3.14.7 \
      numpy=2.5.2 \
      scipy=1.18.0 \
      pyscipopt=6.2.1 \
      cd-hit=4.8.1 \
 && micromamba clean --all --yes

# cvxpy and its solver stack from pip, every transitive dependency pinned so pip
# resolves nothing at build time. numpy/scipy already come from the conda layer.
ENV PATH=/opt/conda/bin:$PATH
ENV PIP_NO_CACHE_DIR=1
RUN pip install --no-cache-dir \
      cvxpy==1.9.2 \
      osqp==1.1.3 \
      clarabel==0.11.1 \
      scs==3.2.11 \
      qdldl==0.1.9.post1 \
      sparsediffpy==0.3.0 \
      highspy==1.15.1 \
      gurobipy==13.0.3

# ── stage 2: Django runtime + Nextflow ───────────────────────────────────────
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Mirrors docker/web/Dockerfile, plus:
#   procps                     Nextflow polls `ps` for per-task CPU/RSS in the trace
#   openjdk-21-jre-headless    Nextflow is a JVM application. 21 rather than
#                              17 because python:3.11-slim is on Debian
#                              trixie, which dropped the 17 packages.
#   git                        `git -C /opt/pipeline rev-parse HEAD` stamps
#                              job.summary["pipeline_commit"] on every run
#   ca-certificates            HTTPS to license.gurobi.com for the WLS check-out,
#                              the one network call a --split_only task makes
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        pkg-config \
        default-libmysqlclient-dev \
        netcat-openbsd \
        curl \
        unzip \
        procps \
        git \
        ca-certificates \
        openjdk-21-jre-headless \
    && rm -rf /var/lib/apt/lists/*

# Pin the launcher: an unpinned `curl get.nextflow.io | bash` makes two builds of
# this Dockerfile potentially different runtimes.
ENV NXF_VER=26.04.0 \
    NXF_HOME=/opt/nextflow \
    JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64
RUN mkdir -p /opt/nextflow \
 && curl -fsSL https://get.nextflow.io | bash \
 && mv nextflow /usr/local/bin/nextflow \
 && chmod +x /usr/local/bin/nextflow \
 # Warm NXF_HOME at build time so the first real run neither downloads the
 # runtime JAR nor the nf-schema plugin. A split then needs the network only for
 # the Gurobi licence check-out, and a registry outage cannot fail a job.
 && nextflow -version \
 && nextflow plugin install nf-schema@2.7.2 \
 && chmod -R a+rX /opt/nextflow

# The pipeline's toolchain. Kept off PATH — see the header.
COPY --from=solver /opt/conda /opt/conda

WORKDIR /code

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY hippie_django ./hippie_django
# Nextflow parameter set, passed with -c. Lives at the repo root, so it is not
# picked up by the COPY above.
COPY conf ./conf

COPY docker/web/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /code/hippie_django

ENTRYPOINT ["/entrypoint.sh"]
# --concurrency=1: a split occupies 2 cpu / 8 GB for up to two hours, which is
# the whole box. A second one running beside it would make both fail on memory
# rather than making either finish sooner.
CMD ["celery", "-A", "hippie", "worker", "-l", "info", "--concurrency=1"]
