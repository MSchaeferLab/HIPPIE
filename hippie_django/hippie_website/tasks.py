import os
import shutil
import signal
import subprocess
import time
import traceback
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from .models import SplitJob
from .services import nextflow_runner as nf
from .services.generate_splits import SplitParams

# How often the task looks up from the subprocess to refresh `step` and check
# for a cancel request. A split runs for tens of minutes to two hours, so this
# is about responsiveness of the Cancel button, not about progress resolution —
# 10 s is well under the time it takes a user to notice they mis-clicked.
POLL_SECONDS = 10

# Grace period between SIGTERM and SIGKILL on cancel. Nextflow handles SIGTERM
# by terminating its running tasks and shutting the executor down, which takes
# longer than a plain process exit but leaves nothing orphaned.
CANCEL_GRACE_SECONDS = 60


def _finish(job: SplitJob, **fields) -> None:
    """Write terminal state in one save, so a poller never sees a half-update."""
    for key, value in fields.items():
        setattr(job, key, value)
    job.save(update_fields=[*fields.keys()])


def _has_live_task(job: SplitJob) -> bool:
    """Is some other worker currently running this job?

    Only the heartbeat can answer that. ``status == "RUNNING"`` on its own
    cannot: it is exactly what a job killed with its container leaves behind,
    and treating that as live would make the row unrunnable forever.
    """
    if job.heartbeat_at is None:
        return False
    timeout = int(getattr(settings, "SPLIT_JOB_HEARTBEAT_TIMEOUT", 15 * 60))
    return job.heartbeat_at > timezone.now() - timedelta(seconds=timeout)


@shared_task(bind=True)
def run_split_job(self, job_id: str):
    """Run one ML-splits job through the Nextflow ppi-splitting-pipeline.

    The splitting itself is entirely the pipeline's: ILP graph partitioning,
    CD-HIT-2D homology filtering, and ILP-optimised negative sampling. What
    stays here is Django's half — turning the stored filters into ppis.csv,
    supervising the run, and packaging what comes back.
    """
    job = SplitJob.objects.get(pk=job_id)

    # Redelivery guard. `acks_late` leaves the message on the broker for the
    # whole run, and Redis re-queues anything still unacked after
    # visibility_timeout — so a run that outlives that window arrives here a
    # second time. Two hours of solver work must not be repeated, and worse, a
    # duplicate would race the original over the same work dir and the same row.
    if job.status in ("DONE", "FAILED"):
        return
    if job.status == "RUNNING" and _has_live_task(job):
        return

    # A job cancelled while still queued must not start. The view also revokes
    # the Celery task, but a worker that had already prefetched this message
    # would run it anyway, so the flag is re-read here rather than trusted to
    # the revoke alone.
    if job.cancel_requested or job.status == "CANCELLED":
        _finish(
            job,
            status="CANCELLED",
            step="cancelled",
            finished_at=timezone.now(),
        )
        return

    layout = nf.job_layout(job_id)

    # Reaching here with status RUNNING means the previous attempt died with its
    # worker (stale heartbeat) and this is a genuine retry. Its half-written
    # ppis.csv and Nextflow work dir would otherwise be inherited by a run that
    # never produced them, so the directory starts empty.
    if job.status == "RUNNING":
        shutil.rmtree(layout.root, ignore_errors=True)

    now = timezone.now()
    job.status = "RUNNING"
    job.step = "exporting_interactions"
    job.started_at = now
    # First beat written with the claim, not at the first poll: the export and
    # the JVM start-up sit between the two, and a null heartbeat during those
    # would make the reaper fall back to started_at for no reason.
    job.heartbeat_at = now
    job.work_dir = str(layout.work)
    job.outdir = str(layout.outdir)
    job.save(
        update_fields=[
            "status",
            "step",
            "started_at",
            "heartbeat_at",
            "work_dir",
            "outdir",
        ]
    )

    proc = None
    try:
        # Before anything expensive: the JVM, the pipeline checkout and the
        # licence either exist or this run is going to die twenty minutes deep
        # inside SOLVE_ILP with an error that names a path in a work dir.
        nf.preflight()

        params = SplitParams.from_payload(job.params)
        export, artifacts_fingerprint = nf.prepare_job_dir(params, layout)
        if export.n_written == 0:
            raise ValueError(
                "The selected filters leave no interactions to split "
                f"({export.n_dropped} were dropped because their accessions are "
                "absent from the precomputed proteome artifacts)."
            )

        run_name = f"hippie_{str(job_id).replace('-', '')[:16]}"
        job.nf_run_name = run_name
        job.step = "starting"
        job.save(update_fields=["nf_run_name", "step"])

        # start_new_session puts nextflow, its JVM and every task child into one
        # process group, so cancelling is a single killpg rather than a hunt for
        # orphaned children. It also detaches the group from Celery's, which is
        # why revoke(terminate=True) is not used for a running job: that would
        # kill the Celery child and leave the JVM running.
        proc = subprocess.Popen(
            nf.build_command(layout, run_name),
            cwd=str(layout.root),
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        last_step = ""
        while proc.poll() is None:
            time.sleep(POLL_SECONDS)

            if SplitJob.objects.filter(pk=job_id, cancel_requested=True).exists():
                _kill_group(proc)
                _finish(
                    job,
                    status="CANCELLED",
                    step="cancelled",
                    finished_at=timezone.now(),
                )
                shutil.rmtree(layout.root, ignore_errors=True)
                return

            # One UPDATE per poll whether or not the step changed — the beat is
            # the liveness signal, and a run that spends 30 minutes inside
            # SOLVE_ILP changes step exactly once in that time.
            step = nf.current_step(layout)
            fields: dict[str, object] = {"heartbeat_at": timezone.now()}
            if step != last_step:
                last_step = step
                fields["step"] = step
            SplitJob.objects.filter(pk=job_id).update(**fields)

        if proc.returncode != 0:
            # Work dir deliberately kept: .command.err and .nextflow.log are the
            # only record of why, and they are gone the moment it is removed.
            _finish(
                job,
                status="FAILED",
                step="failed",
                error=nf.error_report(layout, proc.returncode),
                finished_at=timezone.now(),
            )
            return

        job.step = "packaging"
        job.save(update_fields=["step"])

        summary = nf.collect_summary(
            layout, export, nf.pipeline_commit(), artifacts_fingerprint
        )
        zip_path = nf.package_results(layout, params, summary, job_id)

        _finish(
            job,
            status="DONE",
            step="done",
            progress=1.0,
            zip_path=zip_path,
            summary=summary.__dict__,
            finished_at=timezone.now(),
        )
        # Only on success: nothing in here is needed once the zip exists, and a
        # kept work dir is several GB of staged intermediates per run.
        shutil.rmtree(layout.root, ignore_errors=True)

    except nf.PreflightError as exc:
        # Deployment misconfiguration, not a run that went wrong. The message is
        # the whole of the diagnosis and a traceback around it would only bury
        # it — the run card shows job.error verbatim.
        _finish(
            job,
            status="FAILED",
            step="failed",
            error=str(exc),
            finished_at=timezone.now(),
        )
        raise

    except Exception:
        if proc is not None and proc.poll() is None:
            _kill_group(proc)
        _finish(
            job,
            status="FAILED",
            step="failed",
            error=traceback.format_exc(),
            finished_at=timezone.now(),
        )
        raise


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGTERM the whole process group, SIGKILL it if it does not go quietly."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        proc.wait(timeout=CANCEL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
