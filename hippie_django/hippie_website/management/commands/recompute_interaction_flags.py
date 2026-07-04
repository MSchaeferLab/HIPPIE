"""
Refresh the denormalised ``Interaction.involves_isoform`` flag and the
``n_sources`` / ``n_experiments`` evidence counts.

The default browse view shows canonical proteins only. Reading a single indexed
boolean is far cheaper than two ``protein_*__isoform__isnull`` anti-joins over
the full (1.15M-row) interaction table on every request.

Set ``involves_isoform`` to True for any interaction touching an isoform on
either side. The isoform PK set is small (~7.6k), so the membership UPDATE rides
the ``(protein_1, …)`` / ``(protein_2, …)`` indexes.

``n_sources`` / ``n_experiments`` mirror the M2M edge counts so the browse
interaction table can sort by evidence volume against an indexed scalar column
instead of a per-request GROUP BY/Count over the through tables.

Run after any data import that adds interactions, isoforms, or evidence edges.
"""

import time

from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.db.models import Count, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce

from hippie_website.models import Interaction, Isoform


def _edge_count_subquery(through):
    """Correlated per-interaction edge count over one M2M through table."""
    return Coalesce(
        Subquery(
            through.objects.filter(interaction_id=OuterRef("pk"))
            .values("interaction_id")
            .annotate(c=Count("pk"))
            .values("c")
        ),
        0,
    )


class Command(BaseCommand):
    help = (
        "Recompute Interaction.involves_isoform and the n_sources/"
        "n_experiments evidence counts."
    )

    def handle(self, *args, **options):
        iso_pks = list(Isoform.objects.values_list("protein_ptr_id", flat=True))
        self.stdout.write(f"Found {len(iso_pks)} isoforms; updating flags…")

        # Reset all, then flag the (small) subset touching an isoform.
        Interaction.objects.update(involves_isoform=False)
        flagged = 0
        if iso_pks:
            flagged = Interaction.objects.filter(
                Q(protein_1_id__in=iso_pks) | Q(protein_2_id__in=iso_pks)
            ).update(involves_isoform=True)

        # Refresh denormalised evidence counts from the M2M through tables.
        Interaction.objects.update(
            n_sources=_edge_count_subquery(Interaction.sources.through),
            n_experiments=_edge_count_subquery(Interaction.experiments.through),
        )

        # Invalidate cached browse totals (epoch bump — see views._cached_total).
        cache.set("browse:epoch", int(time.time()))

        self.stdout.write(
            self.style.SUCCESS(
                f"involves_isoform=True for {flagged} interactions; rest False. "
                "Evidence counts refreshed."
            )
        )
