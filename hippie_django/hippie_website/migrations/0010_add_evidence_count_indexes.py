from django.db import migrations, models


class Migration(migrations.Migration):
    """Built after the backfill (0009) so these composite indexes are built
    once against final data instead of incrementally maintained through the
    whole backfill UPDATE."""

    dependencies = [
        ("hippie_website", "0009_backfill_evidence_counts"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="interaction",
            index=models.Index(
                fields=["n_sources", "id"], name="interaction_n_sourc_83710f_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="interaction",
            index=models.Index(
                fields=["n_experiments", "id"], name="interaction_n_exper_4b12d9_idx"
            ),
        ),
    ]
