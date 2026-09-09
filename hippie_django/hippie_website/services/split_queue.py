"""Where a split job sits in the queue, and how long that is likely to be.

The queue is deliberately uncapped and undeduplicated: ten people asking for
ten splits get ten runs, in order. What makes that honest rather than hostile is
telling each of them what the wait actually is, so the numbers here are the
whole of the back-pressure story.

The worker runs ``--concurrency=1``, which is what makes the estimate tractable:
wait = whatever is left of the run in progress + one full run for every job
queued ahead of you.
"""

from __future__ import annotations

import statistics

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from ..models import SplitJob

# How many finished runs the typical-duration estimate looks back over. Small
# on purpose: run time tracks the filters people are actually choosing and the
# size of the database, both of which move between releases, so a long window
# would keep quoting the old release's numbers.
DURATION_SAMPLE = 20

# Quoted before any run has ever finished — and after a prune has removed the
# history. 90 minutes is the middle of the documented "tens of minutes to two
# hours", not a measurement.
DEFAULT_RUN_SECONDS = 90 * 60


def typical_run_seconds() -> int:
    """Median wall-clock of recent successful runs.

    Median, not mean: a single cancelled-at-four-hours outlier would drag a mean
    far enough to make every quoted wait wrong. DONE only — a FAILED run's
    duration is the time to the failure, which says nothing about how long the
    next real one takes.
    """
    fallback = int(
        getattr(settings, "SPLIT_JOB_DEFAULT_RUN_SECONDS", DEFAULT_RUN_SECONDS)
    )
    recent = (
        SplitJob.objects.filter(
            status="DONE", started_at__isnull=False, finished_at__isnull=False
        )
        .order_by("-finished_at")
        .values_list("started_at", "finished_at")[:DURATION_SAMPLE]
    )
    durations = [
        (finished - started).total_seconds()
        for started, finished in recent
        if finished > started
    ]
    if not durations:
        return fallback
    return int(statistics.median(durations))


def queue_position(job: SplitJob) -> int:
    """Jobs still PENDING that were created before this one.

    0 once the job is picked up, and 0 for the job at the head of the queue.
    ``id`` is tie-broken on because it is deterministic, not because a random
    UUID is a real ordinal — without it two jobs sharing a ``created_at`` would
    both report the same position.
    """
    if job.status != "PENDING":
        return 0
    return (
        SplitJob.objects.filter(status="PENDING")
        .filter(
            Q(created_at__lt=job.created_at)
            | Q(created_at=job.created_at, id__lt=job.id)
        )
        .count()
    )


def estimated_wait_seconds(job: SplitJob, position: int | None = None) -> int | None:
    """Seconds until this job is expected to *start*. None once it has.

    Counts what is left of the run in progress rather than a whole one for it,
    so a queued user watching the estimate sees it fall while the job ahead
    finishes instead of dropping in one step. Never negative: a run that has
    already outlasted the typical duration contributes 0, which under-quotes,
    and under-quoting a wait that is visibly still going is better than the
    estimate marching backwards past the run it is waiting on.
    """
    if job.status != "PENDING":
        return None

    position = queue_position(job) if position is None else position
    typical = typical_run_seconds()
    wait = position * typical

    running = (
        SplitJob.objects.filter(status="RUNNING", started_at__isnull=False)
        .order_by("started_at")
        .values_list("started_at", flat=True)
        .first()
    )
    if running is not None:
        elapsed = (timezone.now() - running).total_seconds()
        wait += max(0, typical - elapsed)
    return int(wait)
