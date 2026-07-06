from celery import shared_task
from django.conf import settings
from django.utils import timezone
from pathlib import Path
import shutil
import tempfile
import traceback
from .models import SplitJob
from .services.generate_splits import generate_splits, SplitParams


@shared_task(bind=True)
def run_split_job(self, job_id: str):
    job = SplitJob.objects.get(pk=job_id)
    job.status, job.step = "RUNNING", "starting"
    job.save(update_fields=["status", "step"])

    def cb(step, frac):
        # Throttle DB writes — only on step change or +5% progress
        job.step, job.progress = step, frac
        job.save(update_fields=["step", "progress"])

    try:
        work_dir = Path(tempfile.mkdtemp(prefix=f"split_{job_id}_"))
        summary = generate_splits(SplitParams(**job.params), work_dir, cb)

        zip_base = Path(settings.MEDIA_ROOT) / "splits" / str(job_id)
        zip_base.parent.mkdir(parents=True, exist_ok=True)
        zip_path = shutil.make_archive(str(zip_base), "zip", work_dir)

        job.status = "DONE"
        job.zip_path = zip_path
        job.summary = summary.__dict__
        job.progress = 1.0
        job.finished_at = timezone.now()
        job.save()
    except Exception:
        job.status = "FAILED"
        job.error = traceback.format_exc()
        job.save(update_fields=["status", "error"])
        raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
