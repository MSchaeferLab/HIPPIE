"""
Refresh the denormalised ``Interaction.involves_isoform`` flag.

The default browse view shows canonical proteins only. Reading a single indexed
boolean is far cheaper than two ``protein_*__isoform__isnull`` anti-joins over
the full (1.15M-row) interaction table on every request.

Set to True for any interaction touching an isoform on either side. The isoform
PK set is small (~7.6k), so the membership UPDATE rides the
``(protein_1, …)`` / ``(protein_2, …)`` indexes.

Run after any data import that adds interactions or isoforms.
"""

import time

from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.db.models import Q

from hippie_website.models import Interaction, Isoform


class Command(BaseCommand):
    help = "Recompute Interaction.involves_isoform from the isoform table."

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

        # Invalidate cached browse totals (epoch bump — see views._cached_total).
        cache.set("browse:epoch", int(time.time()))

        self.stdout.write(
            self.style.SUCCESS(
                f"involves_isoform=True for {flagged} interactions; rest False."
            )
        )
