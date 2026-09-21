"""Package a filtered interaction set for standalone use with the public
``ppi-splitting-pipeline`` — the "Download raw data" fast path on the ML
Splits page.

Unlike ``services/nextflow_runner.py``, nothing here runs Nextflow. That module
uses the pipeline's ``--split_only`` mode, which is HIPPIE's own server-side
shortcut: it skips clustering/embedding by feeding in precomputed proteome
artifacts (``data/precomputed/``) nobody outside this deployment has. A
researcher downloading their own raw data instead gets the pipeline's
*standard* mode, which pulls sequences/GO annotations/clustering itself — so
the package here only needs the filtered PPIs (+ curated negatives, if any)
and a plain samplesheet.

Built synchronously by ``views.browse_splits_raw_data`` (no Celery task): the
export is a streaming DB read and a CSV write, nothing like the two-hour
pipeline run, so a click here must never queue behind one.
"""

from __future__ import annotations

import csv
import shutil
import tempfile
from pathlib import Path

from django.conf import settings

from .export_ppis import ExportResult, write_candidate_negatives_csv, write_ppis_csv
from .generate_splits import SplitParams

PIPELINE_WIKI_URL = "https://github.com/bionetslab/ppi-splitting-pipeline/wiki"

# HIPPIE's own split/negative-sampling method choice (conf/hippie.config), used
# here too so a raw-data run and a server-run split agree on methodology.
SPLIT_METHOD = "ilp"
NEGATIVE_SAMPLING_METHOD = "ilp"

# The dataset id for the one samplesheet row. Matches nextflow_runner.DATASET_ID
# in spirit (not imported from there — that constant is scoped to the
# --split_only job's output directory naming, which does not apply here).
DATASET_ID = "hippie"

SAMPLESHEET_FIELDS = [
    "id",
    "ppis",
    "split_method",
    "negative_sampling_method",
    "candidate_network",
]


def pipeline_run_command() -> str:
    """The ``nextflow run`` invocation for this package, shown to the user.

    Identical whether or not curated negatives were found — that distinction
    lives in ``samplesheet.csv``'s ``candidate_network`` cell, not the command
    line.

    The zip has no ``main.nf`` of its own — it ships only the exported data
    plus a samplesheet, meant to be run against a separate
    ppi-splitting-pipeline checkout. Points at ``../ppi-splitting-pipeline/
    main.nf``, the default folder name ``git clone`` gives that repo, while
    still launching from inside the unzipped raw-data folder (so
    ``samplesheet.csv``'s relative paths resolve correctly) — see README's
    "RUN IT" section, which also covers the case where the pipeline checkout
    is named or placed differently.

    ``-profile`` is left listing all four container profiles the pipeline
    ships (``conda``/``docker``/``singularity``/``apptainer``, see the
    pipeline's own ``nextflow.config``) — the researcher swaps it for
    whichever one matches what they have installed before running it.

    No ``--ilp_solver`` override. The pipeline's own default
    (``conf/params.config``: ``ilp_solver=GUROBI``) needs a license most
    researchers running this outside HIPPIE's own deployment will not have,
    and ``processes/splitting.nf`` only omits ``--solver`` from the
    partitioning step when ``params.ilp_solver`` is falsy — an earlier
    version of this command pinned ``--ilp_solver ''`` (empty string, falsy
    in Groovy) to force that fallback onto everyone by default. That also
    silently downgraded the solve for anyone who *does* have a Gurobi
    license, so it is no longer baked into the command; the README instead
    tells researchers without a license to add ``--ilp_solver ''``
    themselves.
    """
    return (
        "nextflow run ../ppi-splitting-pipeline/main.nf "
        "--samplesheet samplesheet.csv --outdir results "
        "-profile conda/docker/singularity/apptainer"
    )


def write_standard_samplesheet(
    dest: Path,
    row_id: str,
    ppis_name: str,
    candidate_name: str | None = None,
) -> Path:
    """One-row samplesheet for the pipeline's *standard* (non-``--split_only``)
    mode.

    Both ``ppis`` and ``candidate_network`` are written as plain filenames —
    paths relative to the samplesheet itself — so the zip is portable. Unlike
    ``nextflow_runner.write_samplesheet``, which deliberately uses absolute
    in-container paths for HIPPIE's own internal job, whoever downloads this
    runs it from wherever they extracted the zip.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(SAMPLESHEET_FIELDS)
        writer.writerow(
            [
                row_id,
                ppis_name,
                SPLIT_METHOD,
                NEGATIVE_SAMPLING_METHOD,
                candidate_name or "",
            ]
        )
    return dest


def _vocab_names(model_path: str, ids) -> list[str]:
    """Resolve stored filter ids to names for the README, best-effort."""
    if not ids:
        return []
    from django.apps import apps

    model = apps.get_model("hippie_website", model_path)
    return sorted(model.objects.filter(pk__in=list(ids)).values_list("name", flat=True))


def write_raw_readme(
    dest: Path,
    params: SplitParams,
    export: ExportResult,
    neg_export: ExportResult,
) -> Path:
    """Human-readable provenance + run instructions shipped inside the zip."""
    sources = _vocab_names("Source", params.source_ids)
    experiments = _vocab_names("ExperimentType", params.experiment_ids)
    types = _vocab_names("InteractionType", params.type_ids)
    tissues = _vocab_names("Tissue", params.tissue_ids)

    def _listing(label: str, names: list[str]) -> str:
        return f"  {label}: {', '.join(names) if names else 'all'}\n"

    has_candidates = neg_export.n_written > 0

    text = [
        "HIPPIE raw data for the ppi-splitting-pipeline\n",
        "===============================================\n\n",
        "This is NOT a finished train/val/test split. It is the filtered\n",
        "interaction set exported as-is, for you to run the public\n",
        "ppi-splitting-pipeline yourself (clone it from\n",
        "https://github.com/bionetslab/ppi-splitting-pipeline).\n\n",
        "FILES\n",
        "  ppis.csv              positives: protein1,protein2,score\n",
    ]
    if has_candidates:
        text.append(
            "  candidate_negatives.csv   curated HIPPIE non-interactions matching\n"
            "                            your filters: protein1,protein2,w\n"
        )
    text += [
        "  samplesheet.csv       ready-made samplesheet — one row, 'hippie'\n\n",
        "RUN IT\n",
        "  Unzip this package so it sits next to your ppi-splitting-pipeline\n",
        "  checkout — both folders as siblings under the same parent directory,\n",
        "  not one nested inside the other. If your pipeline checkout has a\n",
        "  different name or location, edit the main.nf path in the command\n",
        "  below to match (or edit samplesheet.csv's paths yourself to point\n",
        "  elsewhere). Swap -profile for whichever of\n",
        "  conda/docker/singularity/apptainer you have installed, then run this\n",
        "  from inside this folder:\n\n",
        f"    {pipeline_run_command()}\n\n",
        "  No Gurobi license? Add --ilp_solver '' to the command above — it\n",
        "  lets the pipeline auto-select an open-source solver (SCIP/HiGHS)\n",
        "  for the partitioning step instead.\n\n",
    ]
    if has_candidates:
        text.append(
            f"  Negative sampling in this run is restricted to the "
            f"{neg_export.n_written:,} curated HIPPIE non-interactions matching\n"
            "  your filters — wired in as candidate_network in samplesheet.csv.\n"
            "  If you need more negatives than that provides, remove the\n"
            "  candidate_network value in samplesheet.csv (the pipeline will then\n"
            "  sample bias-matched negatives from the full non-interacting pair\n"
            "  space) or set negative_sampling_method to 'uniform' there for\n"
            "  unconstrained random sampling.\n\n"
        )
    else:
        text.append(
            "  No curated HIPPIE non-interactions matched your filters, so\n"
            "  candidate_network is left blank — the pipeline will sample\n"
            "  bias-matched negatives from the full non-interacting pair space.\n\n"
        )
    text += [
        f"  More about the pipeline: {PIPELINE_WIKI_URL}\n\n",
        "FILTERS APPLIED\n",
        f"  score range: {params.min_score} – {params.max_score}\n",
        _listing("sources", sources),
        _listing("experiment types", experiments),
        _listing("interaction types", types),
        _listing("tissues", tissues),
        f"  min TPM: {params.min_tpm}\n",
        f"  min global degree: {params.min_degree_global}\n",
        f"  min average score: {params.min_avg_score}\n",
        f"  isoform mode: {params.isoform_mode}\n\n",
        "COUNTS\n",
        f"  positives exported: {export.n_written}\n",
        f"  positives dropped (accession issues): {export.n_dropped}\n",
        f"  curated negatives exported as candidates: {neg_export.n_written}\n",
    ]
    dest.write_text("".join(text))
    return dest


def package_raw_data(params: SplitParams, job_id) -> tuple[str, dict[str, object]]:
    """Build and zip the raw-data package; returns ``(zip_path, summary_dict)``.

    Everything is written into a throwaway temp directory and zipped straight
    into ``MEDIA_ROOT/splits/<uuid>.zip`` — the same location
    ``nextflow_runner.package_results`` uses, so ``browse_splits_download``
    serves a raw-data job's zip with zero changes.
    """
    with tempfile.TemporaryDirectory(prefix=f"hippie_raw_{job_id}_") as tmp:
        root = Path(tmp)

        export = write_ppis_csv(params, root / "ppis.csv")
        neg_export = write_candidate_negatives_csv(
            params, root / "candidate_negatives.csv"
        )
        has_candidates = neg_export.n_written > 0

        write_standard_samplesheet(
            root / "samplesheet.csv",
            DATASET_ID,
            "ppis.csv",
            "candidate_negatives.csv" if has_candidates else None,
        )
        write_raw_readme(root / "README.txt", params, export, neg_export)

        zip_base = Path(settings.MEDIA_ROOT) / "splits" / str(job_id)
        zip_base.parent.mkdir(parents=True, exist_ok=True)
        zip_path = shutil.make_archive(str(zip_base), "zip", root)

    summary = {
        "interactions_exported": export.n_written,
        "interactions_dropped": export.n_dropped,
        "negatives_exported": neg_export.n_written,
        "pipeline_command": pipeline_run_command(),
    }
    return zip_path, summary
