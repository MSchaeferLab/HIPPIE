"""Write the filtered interaction set to the ``ppis.csv`` the pipeline reads.

Split out from the Celery task because this half is the part worth testing: it
runs against the real ORM, has no Nextflow dependency, and its failure mode is
silent rather than loud. An accession that is absent from the precomputed
artifacts does not crash the pipeline — it produces a protein the KaHIP
partition has no cluster for, and the run either drops it or mis-joins it. So
the drop count is computed here, up front, and recorded in ``job.summary``.
"""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from django.conf import settings

from .generate_splits import (
    SplitParams,
    build_interaction_queryset,
    build_noninteraction_queryset,
)

# The five files a `--split_only` run needs, keyed by the samplesheet column
# that points at each. Built out-of-band once per HIPPIE release (BLAST wants
# 64 cpu / 32 GB, KaHIP 4 cpu / 8 GB) and treated here as a read-only versioned
# artifact — nothing in the web app recomputes them.
#
# all_vs_all.tsv also lives in that directory and is deliberately absent from
# this map: it is the BLAST graph KaHIP was run over, and `--split_only` never
# reads it. At ~470 MB it is by far the largest file there, so listing it would
# make every deploy carry half a gigabyte the pipeline ignores.
ARTIFACT_FILES = {
    "sequences": "sequences.fasta",
    "go_annotations": "go_annotations.tsv",
    "species": "species.tsv",
    "partition": "partitioned_proteome.txt",
    "node_mapping": "node_mapping.tsv",
}


def artifact_dir() -> Path:
    """Directory holding the precomputed pipeline inputs.

    Inside ``hippie_django/data``, which is already bind-mounted into the
    worker, so this needs no volume of its own.
    """
    return Path(settings.BASE_DIR) / "data" / "precomputed"


def artifact_paths(base: Path | None = None) -> dict[str, Path]:
    """Resolve the five artifact paths, failing loudly if any is missing.

    Checked before Nextflow is launched rather than left to the samplesheet
    schema: nf-schema's ``exists: true`` would report the same problem, but
    only after a JVM start, a plugin load and a stack trace, and the message
    would name a samplesheet column instead of the file an operator has to go
    and put back.
    """
    base = base or artifact_dir()
    paths = {key: base / name for key, name in ARTIFACT_FILES.items()}
    missing = sorted(p.name for p in paths.values() if not p.is_file())
    if missing:
        raise FileNotFoundError(
            f"Precomputed pipeline artifacts missing from {base}: "
            f"{', '.join(missing)}. They are built out-of-band once per HIPPIE "
            f"release; see README.md."
        )
    return paths


def load_artifact_accessions(node_mapping: Path) -> set[str]:
    """Accessions the precomputed artifacts cover, read from node_mapping.tsv.

    node_mapping is the right file to read this from rather than the FASTA: it
    is the KaHIP node-id table, so an accession present here is one the
    partition actually assigns a cluster to. It is also ~400 KB against the
    FASTA's 17 MB, and the two agree by construction.
    """
    accessions: set[str] = set()
    with node_mapping.open() as fh:
        reader = csv.reader(fh, delimiter="\t")
        header = next(reader, None)
        # Tolerate a headerless file: if the second column of row 1 does not
        # look like the literal "protein_id", treat row 1 as data.
        if header and len(header) >= 2 and header[1] != "protein_id":
            accessions.add(header[1])
        for row in reader:
            if len(row) >= 2 and row[1]:
                accessions.add(row[1])
    return accessions


def artifact_fingerprint(paths: dict[str, Path]) -> str:
    """A short content hash over the five artifacts, stamped into every run.

    The plan's original idea was to record an artifact *version* in
    ``ReleaseMeta.resource_versions``, but that needs an operator to remember it
    during an out-of-band manual refresh — and the one thing a manual refresh
    reliably loses is the bookkeeping around it. Deriving the identity from the
    files themselves cannot drift from what was actually used.

    Content rather than mtime, so the same proteome copied to a different
    machine fingerprints identically: two splits carrying the same value were
    provably cut from the same proteome, which is exactly the question a
    downloaded split has to be able to answer six months later.

    ~36 MB of hashing, once per run, against a run measured in tens of minutes.
    """
    digest = hashlib.sha256()
    for key in ARTIFACT_FILES:
        digest.update(key.encode())
        with paths[key].open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()[:16]


@dataclass(frozen=True)
class ExportResult:
    """Outcome of one ``ppis.csv`` write."""

    path: Path
    n_written: int
    # Rows dropped because an endpoint's accession is not in the artifacts.
    # Expected to be 0 for a stock release — the artifacts cover all 29,080
    # proteins including the 7,636 isoform accessions — so a non-zero value
    # here means the artifacts and the database have drifted apart.
    n_dropped_unknown: int
    # Rows dropped because an endpoint had a blank accession. Also expected to
    # be 0; kept as a separate counter so a schema regression is not silently
    # attributed to artifact drift.
    n_dropped_blank: int
    n_proteins: int

    @property
    def n_dropped(self) -> int:
        return self.n_dropped_unknown + self.n_dropped_blank


def write_ppis_csv(
    params: SplitParams,
    dest: Path,
    known_accessions: Iterable[str] | None = None,
) -> ExportResult:
    """Stream the filtered interactions to ``dest`` as ``protein1,protein2,score``.

    Column names and order match the ``HIPPIE-current.csv`` the pipeline was
    validated against. Extra columns are preserved through the pipeline, so
    ``score`` survives into the published splits.

    ``.iterator()`` rather than a materialised queryset: an unfiltered run is
    ~1.18 M rows, and holding those as model instances would cost more memory
    than the ILP that follows.
    """
    known = set(known_accessions) if known_accessions is not None else None

    n_written = 0
    n_unknown = 0
    n_blank = 0
    seen: set[str] = set()

    qs = build_interaction_queryset(params).values_list(
        "protein_1__uniprot_accession",
        "protein_2__uniprot_accession",
        "score",
    )

    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["protein1", "protein2", "score"])
        for acc1, acc2, score in qs.iterator(chunk_size=5000):
            if not acc1 or not acc2:
                n_blank += 1
                continue
            if known is not None and (acc1 not in known or acc2 not in known):
                n_unknown += 1
                continue
            writer.writerow([acc1, acc2, score])
            n_written += 1
            seen.add(acc1)
            seen.add(acc2)

    return ExportResult(
        path=dest,
        n_written=n_written,
        n_dropped_unknown=n_unknown,
        n_dropped_blank=n_blank,
        n_proteins=len(seen),
    )


def write_candidate_negatives_csv(params: SplitParams, dest: Path) -> ExportResult:
    """Stream curated ``NonInteraction`` rows to ``dest`` as ``protein1,protein2,w``.

    For the "Download raw data" package (services/raw_export.py): this is the
    file wired into the standard pipeline's ``candidate_network`` samplesheet
    column, which *restricts* the ILP negative sampler's candidate pool to
    exactly these pairs (see ``bin/sample_negatives_ilp.py:load_candidate_network``
    in the ppi-splitting-pipeline). ``w`` is the schema's documented column name
    for the pair weight; only ``protein1``/``protein2`` are actually read by the
    sampler today, but the extra column costs nothing and matches the spec.

    Writes and leaves nothing behind when ``build_noninteraction_queryset``
    returns ``None`` (a source/experiment/type filter is active — a
    NonInteraction can never satisfy one) or when it matches zero rows: the
    caller uses ``n_written == 0`` to omit both the file and the
    ``candidate_network`` samplesheet cell, never to fail the job.
    """
    qs = build_noninteraction_queryset(params)
    if qs is None:
        return ExportResult(
            path=dest, n_written=0, n_dropped_unknown=0, n_dropped_blank=0, n_proteins=0
        )

    n_written = 0
    n_blank = 0
    seen: set[str] = set()

    rows = qs.values_list(
        "protein_1__uniprot_accession",
        "protein_2__uniprot_accession",
        "score",
    )

    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["protein1", "protein2", "w"])
        for acc1, acc2, score in rows.iterator(chunk_size=5000):
            if not acc1 or not acc2:
                n_blank += 1
                continue
            writer.writerow([acc1, acc2, score])
            n_written += 1
            seen.add(acc1)
            seen.add(acc2)

    if n_written == 0:
        dest.unlink(missing_ok=True)

    return ExportResult(
        path=dest,
        n_written=n_written,
        n_dropped_unknown=0,
        n_dropped_blank=n_blank,
        n_proteins=len(seen),
    )


# Header of the samplesheet. Only `id` and `ppis` are required by the schema,
# but `--split_only` additionally demands sequences/go_annotations/species/
# partition/node_mapping on every row (main.nf's buildDatasetsChannel), which is
# exactly the artifact set above. The lambda columns are left off: their values
# come from conf/hippie.config, and a blank samplesheet cell would fall back to
# the same place anyway.
SAMPLESHEET_FIELDS = ["id", "ppis", *ARTIFACT_FILES.keys()]


def write_samplesheet(
    dest: Path,
    row_id: str,
    ppis: Path,
    artifacts: dict[str, Path],
) -> Path:
    """Write the one-row samplesheet describing this job's dataset.

    Absolute paths throughout. Nextflow resolves relative samplesheet paths
    against its launch directory, and the launch directory is the per-job dir
    rather than the pipeline checkout — so a relative path here would resolve
    somewhere neither the artifacts nor ppis.csv actually live.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(SAMPLESHEET_FIELDS)
        writer.writerow(
            [
                row_id,
                str(ppis.resolve()),
                *(str(artifacts[k].resolve()) for k in ARTIFACT_FILES),
            ]
        )
    return dest
