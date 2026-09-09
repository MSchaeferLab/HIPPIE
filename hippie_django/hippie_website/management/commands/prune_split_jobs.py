"""Reap dead ML-split runs and delete the ones past the retention window.

Run this in the **worker** container, not in web: the per-job Nextflow
directories live on the ``nf_work`` volume, which only worker mounts. Run from
web and the zips are collected while every work dir is silently left behind.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandParser

from ...services.split_maintenance import MaintenanceReport, run_maintenance
from ...services.nextflow_runner import run_root


class Command(BaseCommand):
    help = (
        "Mark dead RUNNING split jobs as FAILED, delete terminal jobs older "
        "than the retention window along with their zips and work dirs, and "
        "sweep files whose job row is already gone."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--older-than-days",
            type=int,
            default=None,
            help=(
                "Retention window for terminal jobs. Defaults to "
                "settings.SPLIT_JOB_RETENTION_DAYS "
                f"({getattr(settings, 'SPLIT_JOB_RETENTION_DAYS', 7)})."
            ),
        )
        parser.add_argument(
            "--heartbeat-timeout",
            type=int,
            default=None,
            help=(
                "Seconds without a heartbeat before a RUNNING job is presumed "
                "dead. Defaults to settings.SPLIT_JOB_HEARTBEAT_TIMEOUT "
                f"({getattr(settings, 'SPLIT_JOB_HEARTBEAT_TIMEOUT', 900)})."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be reaped and deleted, change nothing.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        dry_run: bool = bool(options["dry_run"])

        # Running from web rather than worker is the one mistake that leaves the
        # thing this command exists to bound — the Nextflow scratch volume —
        # untouched while still reporting success.
        if not run_root().is_dir():
            self.stderr.write(
                self.style.WARNING(
                    f"{run_root()} does not exist. Work dirs cannot be pruned "
                    "from this container — run this in the worker."
                )
            )

        report: MaintenanceReport = run_maintenance(
            older_than_days=options["older_than_days"],
            timeout_seconds=options["heartbeat_timeout"],
            dry_run=dry_run,
        )
        self._report(report, dry_run)

    def _report(self, report: MaintenanceReport, dry_run: bool) -> None:
        prefix = "would " if dry_run else ""
        lines = [
            f"{prefix}reap (RUNNING -> FAILED): {len(report.reaped)}",
            f"{prefix}prune (terminal, past retention): {len(report.pruned)}",
            f"orphan dirs: {len(report.orphan_dirs)}",
            f"orphan zips: {len(report.orphan_zips)}",
        ]
        if not dry_run:
            lines.append(f"dirs removed: {len(report.removed_dirs)}")
            lines.append(f"zips removed: {len(report.removed_zips)}")
        for line in lines:
            self.stdout.write(line)

        for job_id in report.reaped:
            self.stdout.write(f"  reaped {job_id}")
        for job_id in report.pruned:
            self.stdout.write(f"  pruned {job_id}")

        for err in report.errors:
            self.stderr.write(self.style.ERROR(f"  {err}"))
        if report.errors:
            # Not a raised CommandError: the rows are already gone and the next
            # run's orphan sweep retries the files. Cron should see the noise,
            # not a failure that masks the work that did succeed.
            self.stderr.write(
                self.style.WARNING(
                    f"{len(report.errors)} file(s) could not be removed; the "
                    "next run's orphan sweep will retry them."
                )
            )
