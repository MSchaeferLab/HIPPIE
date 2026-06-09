"""
Refresh the denormalised ``Protein.degree`` / ``Protein.avg_score`` columns.

Browse reads these columns directly instead of aggregating ``interaction`` on
every request.  They are derived once here via three GROUP BYs (one per FK side
plus a self-loop correction) that ride the ``(protein_1, score)`` /
``(protein_2, score)`` covering indexes.

Run after any data import that changes interactions.
"""

import time

from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.db.models import Count, F, Sum

from hippie_website.models import Interaction, Protein


def _group(qs, col: str) -> dict[int, tuple[int, float]]:
    return {
        row[col]: (row["cnt"], row["sm"] or 0.0)
        for row in qs.values(col).annotate(cnt=Count("id"), sm=Sum("score"))
    }


class Command(BaseCommand):
    help = "Recompute Protein.degree and Protein.avg_score from the interaction table."

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch-size",
            type=int,
            default=2000,
            help="Rows per bulk_update batch (default 2000).",
        )

    def handle(self, *args, **options):
        batch_size: int = options["batch_size"]

        self.stdout.write("Aggregating interaction counts…")
        side1 = _group(Interaction.objects.all(), "protein_1_id")
        side2 = _group(Interaction.objects.all(), "protein_2_id")
        self_loops = _group(
            Interaction.objects.filter(protein_1_id=F("protein_2_id")), "protein_1_id"
        )

        to_update: list[Protein] = []
        changed = 0
        total = 0

        proteins = Protein.objects.all().only("pk", "degree", "avg_score").iterator()
        for protein in proteins:
            total += 1
            pid = protein.pk
            c1, s1 = side1.get(pid, (0, 0.0))
            c2, s2 = side2.get(pid, (0, 0.0))
            cl, sl = self_loops.get(pid, (0, 0.0))

            degree = c1 + c2
            unique = c1 + c2 - cl
            avg = round((s1 + s2 - sl) / unique, 4) if unique > 0 else None

            if protein.degree != degree or protein.avg_score != avg:
                protein.degree = degree
                protein.avg_score = avg
                to_update.append(protein)
                changed += 1

            if len(to_update) >= batch_size:
                Protein.objects.bulk_update(to_update, ["degree", "avg_score"])
                to_update.clear()

        if to_update:
            Protein.objects.bulk_update(to_update, ["degree", "avg_score"])

        # Invalidate cached browse totals (degree/avg_score feed min_degree /
        # min_score filters). See views._cached_total.
        cache.set("browse:epoch", int(time.time()))

        self.stdout.write(
            self.style.SUCCESS(
                f"Recomputed stats for {total} proteins ({changed} updated)."
            )
        )
