"""Everything about a Nextflow split run except the subprocess itself.

The subprocess lives in ``tasks.run_split_job`` because it is inseparable from
the Celery job's lifecycle (poll loop, cancel check, status writes). Everything
here — laying out the job directory, building the command line, reading the
pipeline's four output CSVs back into a summary, writing the README — is pure
enough to test without a JVM, which is the whole reason it is not in the task.
"""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from django.conf import settings

from .export_ppis import artifact_paths, write_ppis_csv, write_samplesheet
from .generate_splits import SplitParams

# The dataset id used for the single samplesheet row, and therefore the name of
# the subdirectory the pipeline publishes into (`${params.outdir}/${meta.id}`).
# Constant rather than the job UUID: it lands in every published filename's
# parent directory and in the MultiQC labels, and "hippie" reads better than a
# UUID in a downloaded zip. The job UUID is already the outdir's parent.
DATASET_ID = "hippie"

# What `--split_only` publishes, in the order the README lists them. There is no
# summary.json and no multiqc/ in this mode — both are QC-stage outputs — so the
# summary below is computed from these four files rather than read from disk.
OUTPUT_FILES = ["train.csv", "val.csv", "test_balanced.csv", "test_realistic.csv"]

# Nextflow process name -> the label shown on the run card. Under --split_only
# the pipeline executes exactly these, in this order. SAMPLE_NEGATIVES_DEGREE
# appears alongside SAMPLE_NEGATIVES_ILP because test_realistic is always
# sampled uniformly, even when every other split uses the ILP sampler.
STEP_LABELS = {
    "SORT_PPIS": "sorting_interactions",
    "SOLVE_ILP": "partitioning",
    "CDHIT2D": "clustering_sequences",
    "REMOVE_REDUNDANT": "removing_redundancy",
    "SAMPLE_NEGATIVES_ILP": "sampling_negatives",
    "SAMPLE_NEGATIVES_DEGREE": "sampling_negatives",
}


def run_root() -> Path:
    """Root for per-job Nextflow directories.

    Not under ``MEDIA_ROOT``: Apache serves that directly, and a Nextflow work
    dir contains a staged copy of the Gurobi WLS licence. The compose file
    mounts a dedicated ``nf_work`` volume here.
    """
    return Path(os.environ.get("NF_RUN_ROOT", "/var/nf"))


def pipeline_dir() -> Path:
    """The ppi-splitting-pipeline checkout, mounted read-only."""
    return Path(os.environ.get("PIPELINE_DIR", "/opt/pipeline"))


def gurobi_license() -> Path:
    """The WLS licence file, as the pipeline will see it.

    Same expression ``conf/hippie.config`` evaluates for ``params.gurobi_license``
    (``System.getenv('GRB_LICENSE_FILE') ?: '/etc/gurobi/gurobi.lic'``), so a
    check here is a check of the thing the run will actually stage.
    """
    return Path(os.environ.get("GRB_LICENSE_FILE") or "/etc/gurobi/gurobi.lic")


class PreflightError(RuntimeError):
    """A run cannot possibly succeed, and we knew before starting the JVM."""


def preflight() -> None:
    """Fail a misconfigured run in a second instead of in twenty minutes.

    Every check here is for something that makes SOLVE_ILP die *after* the
    pipeline has loaded the PPIs, built the problem and compiled it — a quarter
    of an hour, times the retry ladder, ending in an error that names a path
    inside a work dir nobody has ever seen. The cost of the run is not in
    starting it, so it is worth refusing to.
    """
    main_nf = pipeline_dir() / "main.nf"
    if not main_nf.is_file():
        raise PreflightError(
            f"The pipeline checkout at {pipeline_dir()} has no main.nf. On the "
            "server this almost always means the submodule was never fetched: "
            "run `git submodule update --init` and rebuild. Note that a plain "
            "`git pull` does not update a submodule."
        )

    config = hippie_config()
    if not config.is_file():
        raise PreflightError(
            f"The pipeline parameter file {config} is missing. It is COPYed "
            "into the worker image from conf/hippie.config; a worker built "
            "before that file existed will not have it."
        )

    licence = gurobi_license()
    if licence.is_dir():
        # Docker creates a *directory* at both ends of a bind mount whose host
        # path does not exist, and Nextflow's `checkIfExists` is satisfied by
        # one. It then stages the directory into the task work dir, and Gurobi
        # is the first thing in the chain that notices.
        raise PreflightError(
            f"{licence} is a directory, not a file. Docker created it that way "
            "because the host path in the GUROBI_LICENSE_PATH bind mount does "
            "not exist. On the server: `docker compose down`, remove the empty "
            f"directory, put the real WLS licence at the host path (default "
            "./secrets/gurobi.lic) or point GUROBI_LICENSE_PATH at wherever it "
            "actually is, then `docker compose up -d`."
        )
    if not licence.is_file():
        raise PreflightError(
            f"No Gurobi licence at {licence}. Both solvers are pinned to Gurobi "
            "with no fallback, so a run without one cannot complete. Mount a WLS "
            "licence there (GUROBI_LICENSE_PATH in .env)."
        )
    if licence.stat().st_size == 0:
        raise PreflightError(
            f"The Gurobi licence at {licence} is empty. A truncated or "
            "placeholder licence fails the same way a missing one does, only "
            "twenty minutes later."
        )


def hippie_config() -> Path:
    """HIPPIE's hard-coded parameter file, passed to Nextflow with ``-c``."""
    override = os.environ.get("NF_HIPPIE_CONFIG")
    if override:
        return Path(override)
    # settings.BASE_DIR is hippie_django/; conf/ sits beside it at the repo root
    # and is COPYed to /code/conf in the worker image.
    return Path(settings.BASE_DIR).parent / "conf" / "hippie.config"


def pipeline_commit(pipeline: Path | None = None) -> str:
    """Resolved commit of the mounted pipeline checkout.

    Under the chosen runner the pipeline's ``bin/`` is mounted, not baked, so
    the checkout's commit is a direct result-determining input to every split.
    Recording it is the difference between an assumed reproducibility guarantee
    and an actual one. Best-effort: a checkout without git metadata (an archive
    export, say) still produces a usable run, so this degrades to "unknown"
    rather than failing the job.
    """
    pipeline = pipeline or pipeline_dir()
    try:
        out = subprocess.run(
            # -c safe.directory: the checkout is a bind mount owned by the host
            # user while the container runs as root, which trips git's dubious
            # ownership check and would silently degrade every run's provenance
            # stamp to "unknown". Scoped to this one path and this one
            # invocation — nothing is written to a git config.
            [
                "git",
                "-c",
                f"safe.directory={pipeline}",
                "-C",
                str(pipeline),
                "rev-parse",
                "HEAD",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() or "unknown"


@dataclass(frozen=True)
class JobLayout:
    """The directories one split run owns."""

    root: Path  # <NF_RUN_ROOT>/<job-uuid>
    work: Path  # -work-dir; deleted on success/cancel, kept on failure
    outdir: Path  # --outdir; the pipeline publishes into outdir/<DATASET_ID>
    samplesheet: Path
    ppis: Path
    log: Path  # .nextflow.log, written into root (the launch dir)
    trace: Path  # trace.txt, ditto — see conf/hippie.config

    @property
    def results(self) -> Path:
        """Where the four published CSVs actually land."""
        return self.outdir / DATASET_ID


def job_layout(job_id: str, root: Path | None = None) -> JobLayout:
    base = (root or run_root()) / str(job_id)
    return JobLayout(
        root=base,
        work=base / "work",
        outdir=base / "results",
        samplesheet=base / "samplesheet.csv",
        ppis=base / "ppis.csv",
        log=base / ".nextflow.log",
        trace=base / "trace.txt",
    )


def prepare_job_dir(
    params: SplitParams, layout: JobLayout, artifacts_base: Path | None = None
) -> tuple[object, str]:
    """Create the job directory and write ppis.csv + the samplesheet into it.

    Returns ``(ExportResult, artifacts_fingerprint)`` — the drop count and the
    identity of the proteome the split was cut from, both of which end up in
    ``job.summary``.
    """
    from .export_ppis import artifact_fingerprint, load_artifact_accessions

    layout.root.mkdir(parents=True, exist_ok=True)
    artifacts = artifact_paths(artifacts_base)
    known = load_artifact_accessions(artifacts["node_mapping"])
    export = write_ppis_csv(params, layout.ppis, known_accessions=known)
    write_samplesheet(layout.samplesheet, DATASET_ID, layout.ppis, artifacts)
    return export, artifact_fingerprint(artifacts)


def build_command(
    layout: JobLayout, run_name: str, pipeline: Path | None = None
) -> list[str]:
    """The ``nextflow run`` argv for one job.

    No ``-resume`` and no shared work dir: runs are not deduplicated, so a
    resume could only ever pick up this job's own failed attempt, and keeping
    the option would mean keeping every work dir forever to make it meaningful.

    ``-ansi-log false`` matters more than it looks — with the default the log is
    a stream of cursor-movement escapes rather than lines, which makes the tail
    captured into ``job.error`` unreadable.
    """
    pipeline = pipeline or pipeline_dir()
    return [
        "nextflow",
        "-log",
        str(layout.log),
        "run",
        str(pipeline / "main.nf"),
        "-name",
        run_name,
        "-c",
        str(hippie_config()),
        "-ansi-log",
        "false",
        "-work-dir",
        str(layout.work),
        "--samplesheet",
        str(layout.samplesheet),
        "--outdir",
        str(layout.outdir),
        "--split_only",
        "true",
    ]


# `[bf/ede9a7] Submitted process > PPI_SPLITTING:SPLIT_POSITIVES:SOLVE_ILP (hippie)`
# — the name is fully qualified by the enclosing workflows, so only the final
# segment matches STEP_LABELS.
_SUBMITTED_RE = re.compile(r"Submitted process > ([A-Za-z0-9_:]+)")

# How much of the tail of .nextflow.log to scan. The log is a debug log and
# reaches tens of MB on a long run, and this is re-read every poll.
_LOG_TAIL_BYTES = 256 * 1024


def current_step(layout: JobLayout) -> str:
    """Coarse step label, read from the tail of .nextflow.log.

    The log rather than the trace, because the trace only gains a row when a
    task *finishes*: a run spending 30 minutes inside SOLVE_ILP would report
    "starting" for the whole of it. The log's "Submitted process >" line is
    written when the task is launched, which is the moment the label should
    change.

    Deliberately not a percentage. A fake fraction reads worse than an honest
    "sampling negatives, running for 41 min" — the ILP steps are bounded by
    their own solver budget, not by how many tasks have finished, so a
    task-count fraction would sit still for an hour and read as a hang.
    """
    if not layout.log.is_file():
        return "starting"
    try:
        with layout.log.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - _LOG_TAIL_BYTES))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return "starting"

    names = _SUBMITTED_RE.findall(tail)
    if not names:
        return "starting"
    # PPI_SPLITTING is the top-level workflow, submitted before any real task.
    for qualified in reversed(names):
        leaf = qualified.rsplit(":", 1)[-1]
        if leaf in STEP_LABELS:
            return STEP_LABELS[leaf]
    return "starting"


def _read_split(path: Path) -> tuple[dict[str, object], set[str]]:
    """Row counts and the protein set for one published split CSV.

    The pipeline emits a single labelled file per split (``protein1,protein2,
    label`` plus any columns carried through from ppis.csv), which is the shape
    change the UI has to absorb: it replaces today's ``<split>_pos.csv`` +
    ``<split>_neg.csv`` pair.

    The protein set is returned rather than only its size so the caller can
    union the four without a second pass — at ~1 M rows per file on an
    unfiltered run, re-parsing them to count distinct proteins across splits is
    the most expensive thing this module would otherwise do.
    """
    n_pos = n_neg = 0
    proteins: set[str] = set()
    with path.open() as fh:
        for row in csv.DictReader(fh):
            label = (row.get("label") or "").strip()
            if label == "1":
                n_pos += 1
            elif label == "0":
                n_neg += 1
            for key in ("protein1", "protein2"):
                value = (row.get(key) or "").strip()
                if value:
                    proteins.add(value)
    stats = {
        "name": path.stem,
        "n_pos": n_pos,
        "n_neg": n_neg,
        "n_proteins": len(proteins),
    }
    return stats, proteins


@dataclass
class RunSummary:
    """What a finished run reports back to the UI and writes into summary.json."""

    n_proteins: int = 0
    n_positive_total: int = 0
    n_negative_total: int = 0
    # Interactions the exporter dropped before the run — see ExportResult. Not
    # the same thing as the old n_discarded_edges, which counted edges the
    # graph cut threw away.
    interactions_exported: int = 0
    interactions_dropped: int = 0
    pipeline_commit: str = "unknown"
    # Content hash of the five precomputed proteome artifacts — see
    # export_ppis.artifact_fingerprint. Two splits sharing this value were cut
    # from the same proteome.
    artifacts_fingerprint: str = "unknown"
    splits: list = field(default_factory=list)


def collect_summary(
    layout: JobLayout, export, commit: str, artifacts_fingerprint: str = "unknown"
) -> RunSummary:
    """Read the four published CSVs into a summary.

    Fails loudly on a missing file. A run that exits 0 without publishing all
    four is a pipeline contract break, and reporting DONE on three splits would
    hand the user a silently incomplete dataset.
    """
    missing = [f for f in OUTPUT_FILES if not (layout.results / f).is_file()]
    if missing:
        raise FileNotFoundError(
            f"Pipeline exited successfully but did not publish {', '.join(missing)} "
            f"in {layout.results}"
        )
    splits = []
    all_proteins: set[str] = set()
    for name in OUTPUT_FILES:
        stats, proteins = _read_split(layout.results / name)
        splits.append(stats)
        all_proteins |= proteins

    return RunSummary(
        n_proteins=len(all_proteins),
        n_positive_total=sum(int(s["n_pos"]) for s in splits),
        n_negative_total=sum(int(s["n_neg"]) for s in splits),
        interactions_exported=export.n_written,
        interactions_dropped=export.n_dropped,
        pipeline_commit=commit,
        artifacts_fingerprint=artifacts_fingerprint,
        splits=splits,
    )


def _vocab_names(model_path: str, ids) -> list[str]:
    """Resolve stored filter ids to names for the README, best-effort."""
    if not ids:
        return []
    from django.apps import apps

    model = apps.get_model("hippie_website", model_path)
    return sorted(model.objects.filter(pk__in=list(ids)).values_list("name", flat=True))


def write_readme(dest: Path, params: SplitParams, summary: RunSummary) -> Path:
    """Human-readable provenance shipped inside the zip.

    The point is that a downloaded split is separable from the UI that made it:
    six months later the zip has to answer "what was filtered, and by which
    pipeline" on its own.
    """
    sources = _vocab_names("Source", params.source_ids)
    experiments = _vocab_names("ExperimentType", params.experiment_ids)
    types = _vocab_names("InteractionType", params.type_ids)
    tissues = _vocab_names("Tissue", params.tissue_ids)

    def _listing(label: str, names: list[str]) -> str:
        return f"  {label}: {', '.join(names) if names else 'all'}\n"

    text = [
        "HIPPIE machine-learning splits\n",
        "=============================\n\n",
        "Generated by the ppi-splitting-pipeline (--split_only) from a filtered\n",
        "subset of the HIPPIE interaction set.\n\n",
        "FILES\n",
        "  train.csv            positives + ILP-sampled negatives, 1:1\n",
        "  val.csv              same, 1:1\n",
        "  test_balanced.csv    same, 1:1\n",
        "  test_realistic.csv   positives + UNIFORM negatives, 1:10 — simulates an\n",
        "                       uncontrolled screen, so it is deliberately not\n",
        "                       bias-matched and is identical for every negative set\n\n",
        "  Columns: protein1,protein2,label[,score]   label 1 = interacting, 0 = not.\n\n",
        "  Splits are leakage-aware by sequence homology, not only by graph cut:\n",
        "  CD-HIT-2D plus a redundancy filter remove train/test-similar pairs, so\n",
        "  the counts below are lower than the raw filtered interaction count.\n\n",
        "FILTERS APPLIED\n",
        f"  score range: {params.min_score} – {params.max_score}\n",
        _listing("sources", sources),
        _listing("experiment types", experiments),
        _listing("interaction types", types),
        _listing("tissues", tissues),
        f"  min RPKM: {params.min_rpkm}\n",
        f"  min global degree: {params.min_degree_global}\n",
        f"  min average score: {params.min_avg_score}\n",
        f"  isoform mode: {params.isoform_mode}\n\n",
        "PIPELINE PARAMETERS (fixed, see conf/hippie.config)\n",
        "  split_method=ilp  negative_sampling_method=ilp  seed=42\n",
        "  train/val/test = 0.8 / 0.1 / 0.1   ilp_epsilon=0.05\n",
        "  cdhit_identity=0.4  cdhit_wordsize=2\n",
        "  ilp_solver=GUROBI  ilp_max_sec=1800\n",
        "  neg_ilp_solver=gurobi  neg_ilp_time_limit=1800  neg_ilp_mip_gap=0.01\n",
        "  neg_ilp_lambda_degree / _taxon_pair / _self_loop / _jaccard = 1.0\n\n",
        f"  pipeline commit: {summary.pipeline_commit}\n",
        f"  proteome artifacts: {summary.artifacts_fingerprint}\n"
        "    (content hash of the five precomputed inputs; two splits sharing it\n"
        "     were cut from the same proteome)\n\n",
        "COUNTS\n",
        f"  interactions exported to the pipeline: {summary.interactions_exported}\n",
        f"  interactions dropped (accession absent from the precomputed\n"
        f"    proteome artifacts): {summary.interactions_dropped}\n",
        f"  distinct proteins across all splits: {summary.n_proteins}\n",
        f"  positives: {summary.n_positive_total}   negatives: {summary.n_negative_total}\n",
    ]
    for s in summary.splits:
        text.append(
            f"    {s['name']:<18} {s['n_pos']:>9} pos  {s['n_neg']:>9} neg  "
            f"{s['n_proteins']:>7} proteins\n"
        )
    dest.write_text("".join(text))
    return dest


def package_results(
    layout: JobLayout, params: SplitParams, summary: RunSummary, job_id: str
) -> str:
    """Write summary.json + README.txt beside the CSVs and zip the lot.

    Zips into ``MEDIA_ROOT/splits/<uuid>.zip`` — the one artifact that outlives
    the job directory, and the only thing Apache serves.
    """
    (layout.results / "summary.json").write_text(json.dumps(summary.__dict__, indent=2))
    write_readme(layout.results / "README.txt", params, summary)

    zip_base = Path(settings.MEDIA_ROOT) / "splits" / str(job_id)
    zip_base.parent.mkdir(parents=True, exist_ok=True)
    return shutil.make_archive(str(zip_base), "zip", layout.results)


def error_report(layout: JobLayout, returncode: int, tail_lines: int = 60) -> str:
    """Assemble a readable failure message from the run's own artifacts.

    The Nextflow log's tail names the failing process; that process's
    ``.command.err`` is where the solver or the Python traceback actually is.
    Both are worth having, because "SAMPLE_NEGATIVES_ILP terminated with an
    error exit status (1)" on its own says nothing about whether the cause was
    a licence check-out, an OOM kill, or a genuine infeasibility.
    """
    parts = [f"nextflow exited with status {returncode}"]

    if layout.log.is_file():
        try:
            lines = layout.log.read_text(errors="replace").splitlines()
            parts.append(
                "\n--- .nextflow.log (tail) ---\n" + "\n".join(lines[-tail_lines:])
            )
        except OSError:
            pass

    # Newest .command.err with content — the failing task's, in practice, since
    # a successful task leaves an empty one.
    errs = []
    if layout.work.is_dir():
        for p in layout.work.rglob(".command.err"):
            try:
                if p.stat().st_size > 0:
                    errs.append((p.stat().st_mtime, p))
            except OSError:
                continue
    if errs:
        _, newest = max(errs)
        try:
            lines = newest.read_text(errors="replace").splitlines()
            parts.append(
                f"\n--- {newest.parent.name}/.command.err (tail) ---\n"
                + "\n".join(lines[-tail_lines:])
            )
        except OSError:
            pass

    return "\n".join(parts)
