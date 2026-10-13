# Distinguishes a real pipeline RUN (queued, Celery, up to 2h) from a
# RAW_DATA job (the "Download raw data" button — built synchronously in the
# request, never touches the Celery queue). See services/raw_export.py.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("hippie_website", "0009_rename_median_rpkm_to_median_tpm"),
    ]

    operations = [
        migrations.AddField(
            model_name="splitjob",
            name="job_type",
            field=models.CharField(
                choices=[("RUN", "RUN"), ("RAW_DATA", "RAW_DATA")],
                default="RUN",
                max_length=10,
            ),
        ),
    ]
