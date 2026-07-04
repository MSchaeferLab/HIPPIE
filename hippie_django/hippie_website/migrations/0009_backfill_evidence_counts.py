from django.db import migrations
from django.db.models import Count, OuterRef, Subquery
from django.db.models.functions import Coalesce


def _backfill_counts(apps, schema_editor):
    """Populate n_sources/n_experiments from the M2M through tables (one
    correlated UPDATE per column). Mirrors ``recompute_interaction_flags``;
    safe to re-run. Runs before the composite indexes (0010) are built so
    they're built once against final data instead of incrementally
    maintained through the whole backfill."""
    Interaction = apps.get_model("hippie_website", "Interaction")

    def edge_count(through):
        return Coalesce(
            Subquery(
                through.objects.filter(interaction_id=OuterRef("pk"))
                .values("interaction_id")
                .annotate(c=Count("pk"))
                .values("c")
            ),
            0,
        )

    Interaction.objects.update(
        n_sources=edge_count(Interaction.sources.through),
        n_experiments=edge_count(Interaction.experiments.through),
    )


class Migration(migrations.Migration):
    dependencies = [
        (
            "hippie_website",
            "0008_interaction_n_experiments_interaction_n_sources_and_more",
        ),
    ]

    operations = [
        migrations.RunPython(_backfill_counts, migrations.RunPython.noop),
    ]
