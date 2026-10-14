# AlphaFold DB complex-prediction id per interaction, populated by
# `hippie_update --alphafold FILE`. Nullable add — no data migration needed.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("hippie_website", "0010_splitjob_job_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="interaction",
            name="alphafold_model_id",
            field=models.CharField(blank=True, max_length=32, null=True),
        ),
    ]
