"""Housekeeping for ML-split jobs: reaping dead runs and pruning old ones.

Nothing here runs on its own — ``manage.py prune_split_jobs`` is the entry
point, and the deployment cron is what calls it. It is not a Celery task on
purpose: the worker runs ``--concurrency=1`` and a split holds that slot for
hours, so a queued housekeeping task would sit behind every run and never get
to clean up on a busy day.

Two jobs, both idempotent:

*Reaping* turns a RUNNING row whose task is dead into a FAILED one. Without it
a worker that is OOM-killed or replaced mid-run leaves a row that polls forever
and is counted by nothing.

*Pruning* deletes terminal jobs past the retention window together with their
zip and any kept work dir, and then sweeps files whose job row is already gone.
"""

from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from ..models import SplitJob
from .nextflow_runner import job_layout, run_root

# Jobs in these states are finished with; only they are ever pruned by age.
# PENDING and RUNNING are excluded deliberately — the queue is uncapped, so a
# job can legitimately be days old and still be waiting its turn.
TERMINAL_STATUSES = ("DONE", "FAILED", "CANCELLED")

REAPED_ERROR = (
    "The worker running this split stopped without reporting a result "
    "(no heartbeat for {seconds}s). The run was almost certainly killed with "
    "its container — resubmit it."
)


def _retention_days() -> int:
    return int(getattr(settings, "SPLIT_JOB_RETENTION_DAYS", 7))


def _heartbeat_timeout() -> int:
    return int(getattr(settings, "SPLIT_JOB_HEARTBEAT_TIMEOUT", 15 * 60))


def splits_dir() -> Path:
    """Where ``package_results`` puts the one artifact that outlives a run."""
    return Path(settings.MEDIA_ROOT) / "splits"


@dataclass
class MaintenanceReport:
    reaped: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    removed_dirs: list[str] = field(default_factory=list)
    removed_zips: list[str] = field(default_factory=list)
    orphan_dirs: list[str] = field(default_factory=list)
    orphan_zips: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Reaping
# ---------------------------------------------------------------------------


def reap_stale_jobs(
    report: MaintenanceReport, timeout_seconds: int | None = None, dry_run: bool = False
) -> MaintenanceReport:
    """Mark RUNNING jobs whose task has stopped heartbeating as FAILED.

    ``heartbeat_at`` is null for rows written before the field existed and for
    the brief window between the task claiming the job and its first poll, so
    those fall back to ``started_at``. A row with neither is not touched: it has
    no timestamp to be stale against, and guessing would risk failing a live run.
    """
    timeout = timeout_seconds if timeout_seconds is not None else _heartbeat_timeout()
    cutoff = timezone.now() - timedelta(seconds=timeout)

    stale = SplitJob.objects.filter(status="RUNNING").filter(
        Q(heartbeat_at__lt=cutoff) | Q(heartbeat_at__isnull=True, started_at__lt=cutoff)
    )
    for job in stale:
        report.reaped.append(str(job.id))
        if dry_run:
            continue
        job.status = "FAILED"
        job.step = "failed"
        job.error = REAPED_ERROR.format(seconds=timeout)
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "step", "error", "finished_at"])
    return report


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------


def _job_root(job: SplitJob) -> Path | None:
    """The per-job Nextflow directory, or None if it cannot be named safely.

    ``work_dir`` is an absolute in-container path recorded at run time, which is
    authoritative even if ``NF_RUN_ROOT`` has since changed; ``job_layout`` is
    the fallback for a job that never got as far as writing it. Either way the
    directory name has to be the job's own UUID before anything is removed —
    this function hands a path to ``rmtree``, so a malformed row must not be
    able to aim it somewhere else.
    """
    candidate = Path(job.work_dir).parent if job.work_dir else job_layout(job.id).root
    return candidate if candidate.name == str(job.id) else None


def _remove_dir(path: Path, report: MaintenanceReport, into: list[str]) -> None:
    if not path.is_dir():
        return
    try:
        shutil.rmtree(path)
        into.append(str(path))
    except OSError as exc:
        report.errors.append(f"could not remove {path}: {exc}")


def _remove_file(path: Path, report: MaintenanceReport, into: list[str]) -> None:
    if not path.is_file():
        return
    try:
        path.unlink()
        into.append(str(path))
    except OSError as exc:
        report.errors.append(f"could not remove {path}: {exc}")


def prune_old_jobs(
    report: MaintenanceReport, older_than_days: int | None = None, dry_run: bool = False
) -> MaintenanceReport:
    """Delete terminal jobs past the retention window, files first.

    Cut-off is ``finished_at``, falling back to ``created_at`` for a terminal
    row that somehow never got one. Files go before the row so that a failure to
    unlink leaves a row still pointing at them; the orphan sweep is the backstop
    for the other order.
    """
    days = older_than_days if older_than_days is not None else _retention_days()
    cutoff = timezone.now() - timedelta(days=days)

    old = SplitJob.objects.filter(status__in=TERMINAL_STATUSES).filter(
        Q(finished_at__lt=cutoff) | Q(finished_at__isnull=True, created_at__lt=cutoff)
    )
    for job in old:
        report.pruned.append(str(job.id))
        if dry_run:
            continue
        root = _job_root(job)
        if root is not None:
            _remove_dir(root, report, report.removed_dirs)
        if job.zip_path:
            _remove_file(Path(job.zip_path), report, report.removed_zips)
        # Belt and braces: package_results always writes this exact name, so a
        # row whose zip_path was never persisted still gets its zip collected.
        _remove_file(splits_dir() / f"{job.id}.zip", report, report.removed_zips)
        job.delete()
    return report


def _known_job_ids() -> set[str]:
    return {str(pk) for pk in SplitJob.objects.values_list("pk", flat=True)}


def _is_uuid(name: str) -> bool:
    try:
        uuid.UUID(name)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def sweep_orphans(
    report: MaintenanceReport, dry_run: bool = False
) -> MaintenanceReport:
    """Remove run directories and zips with no SplitJob row behind them.

    These accumulate from any path that deletes a row without its files — a
    manual ``delete()`` in the admin, a prune interrupted between the two, a
    database restored from a backup older than the volume. Only UUID-named
    entries are considered, so nothing else that shares the directory survives
    on luck.
    """
    known = _known_job_ids()

    root = run_root()
    if root.is_dir():
        for entry in root.iterdir():
            if not entry.is_dir() or not _is_uuid(entry.name) or entry.name in known:
                continue
            report.orphan_dirs.append(str(entry))
            if not dry_run:
                _remove_dir(entry, report, report.removed_dirs)

    zips = splits_dir()
    if zips.is_dir():
        for entry in zips.glob("*.zip"):
            if not _is_uuid(entry.stem) or entry.stem in known:
                continue
            report.orphan_zips.append(str(entry))
            if not dry_run:
                _remove_file(entry, report, report.removed_zips)

    return report


def run_maintenance(
    older_than_days: int | None = None,
    timeout_seconds: int | None = None,
    dry_run: bool = False,
) -> MaintenanceReport:
    """Reap, then prune, then sweep — in that order, which matters.

    Reaping first means a run that died inside the retention window is already
    terminal by the time pruning looks at it, so it is eligible in this pass
    rather than staying RUNNING forever and never being collected at all.
    """
    report = MaintenanceReport()
    reap_stale_jobs(report, timeout_seconds=timeout_seconds, dry_run=dry_run)
    prune_old_jobs(report, older_than_days=older_than_days, dry_run=dry_run)
    sweep_orphans(report, dry_run=dry_run)
    return report
