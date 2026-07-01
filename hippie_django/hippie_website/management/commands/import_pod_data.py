from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from django.core.management.base import BaseCommand, CommandParser

from ._sources import data_path

if TYPE_CHECKING:
    pass


def _parse_pmids(raw: str) -> set[int]:
    """Parse 'PMID1_METHOD;PMID2_METHOD' into a set of integer PMIDs."""
    pmids: set[int] = set()
    for entry in str(raw).split(";"):
        part = entry.split("_")[0].strip()
        if part.isdigit():
            pmids.add(int(part))
    return pmids


class Command(BaseCommand):
    help = "Import BaitPreyAssociation (and NonInteraction) records from POD_flat.pq"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--file",
            default=str(data_path("pod_flat")),
            help="Path to parquet file (default: from data/sources.json)",
        )
        parser.add_argument(
            "--min-tests",
            type=int,
            default=5,
            help="Minimum n_tested to create a NonInteraction when n_observed == 0 (default: 5)",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=10_000,
            help="Rows per parquet read batch (default: 10000)",
        )


    def handle(self, *args: object, **options: object) -> None:
        import pyarrow.parquet as pq
        from django.db import transaction
        from django.db.models import F

        from hippie_website.models import (
            BaitPreyAssociation,
            Interaction,
            NonInteraction,
            Protein,
            Publication,
        )

        file_path: str = str(options["file"])
        min_tests: int = int(options["min_tests"])  # type: ignore[arg-type]
        batch_size: int = int(options["batch_size"])  # type: ignore[arg-type]

        self.stdout.write(f"Opening {file_path}")

        # --- Phase 1: Pre-fetch all proteins and interactions ---
        protein_map: dict[str, int] = dict(
            Protein.objects.values_list("uniprot_accession", "id")
        )
        self.stdout.write(f"Loaded {len(protein_map):,} proteins")

        interactions: dict[tuple[int, int], Interaction] = {
            (r.protein_1_id, r.protein_2_id): r
            for r in Interaction.objects.all()
        }
        interaction_pairs: set[tuple[int, int]] = set(interactions.keys())
        self.stdout.write(f"Loaded {len(interactions):,} interactions")

        # --- Phase 2: Stream parquet, accumulate only qualifying pairs ---
        groups: dict[tuple[int, int], dict[str, object]] = defaultdict(
            lambda: {"interaction": False, "n_tested": 0, "n_observed": 0, "pmids": set()}
        )

        rows_read = 0
        skipped_missing_protein = 0
        skipped_rejected = 0
        lines_parsed = 0

        parquet_file = pq.ParquetFile(file_path)
        self.stdout.write("Reading input test/observation data ...")
        for batch in parquet_file.iter_batches(batch_size=batch_size):
            df = batch.to_pandas()
            for row in df.itertuples(index=False):
                rows_read += 1
                bait_pk = protein_map.get(row.uniprot_id_bait)
                prey_pk = protein_map.get(row.uniprot_id_prey)
                if bait_pk is None or prey_pk is None:
                    skipped_missing_protein += 1
                    continue

                
                key = tuple(sorted([bait_pk, prey_pk]))
                p1_id, p2_id = key
                
                
                entry = groups[key]
                if key in interaction_pairs:
                    entry["n_tested"] = int(row.n_tested)
                    entry["n_observed"] = int(row.n_observed)
                    entry["pmids"] |= _parse_pmids(row.pubmed_id)  
                    entry["interaction"] = True
                elif int(row.n_observed) == 0 and int(row.n_tested) >= min_tests:
                    entry["n_tested"] = int(row.n_tested)
                    entry["n_observed"] = int(row.n_observed)
                    entry["pmids"] |= _parse_pmids(row.pubmed_id)  
                else:
                    del groups[key]
                    skipped_rejected += 1
                    continue

            lines_parsed += batch_size
            self.stdout.write(
                f"Parsed: {lines_parsed:,} lines — "
                f"keeping {len(groups):,} pairs, rejected {skipped_rejected:,}"
            )

        self.stdout.write(
            f"Read {rows_read:,} rows → {len(groups):,} qualifying pairs "
            f"({skipped_missing_protein:,} skipped: protein not in DB, "
            f"{skipped_rejected:,} skipped: failing non-interaction critera)"
        )

        # --- Phase 3: Bulk-fetch existing NonInteractions for qualifying pairs ---
        non_interaction_keys = {k for k in groups if not groups[k]["interaction"]}
        p1_ids = {p for p, _ in non_interaction_keys}
        p2_ids = {p for _, p in non_interaction_keys}
        existing_noninteractions: dict[tuple[int, int], NonInteraction] = {
            (r.protein_1_id, r.protein_2_id): r
            for r in NonInteraction.objects.filter(
                protein_1_id__in=p1_ids, protein_2_id__in=p2_ids
            )
        }

        # --- Phase 4: Bulk-upsert Publications ---
        all_pmids: set[int] = set()
        for entry in groups.values():
            all_pmids |= entry["pmids"]  # type: ignore[operator]

        Publication.objects.bulk_create(
            [Publication(pmid=p) for p in all_pmids],
            ignore_conflicts=True,
        )
        pub_map: dict[int, Publication] = {
            p.pmid: p for p in Publication.objects.filter(pmid__in=all_pmids)
        }

        # --- Phase 5: Create/update BaitPreyAssociation ---
        skipped_no_target = 0
        noninteractions_created = 0
        assocs_created = 0
        assocs_updated = 0

        with transaction.atomic():
            for (p1_id, p2_id), data in groups.items():
                n_tested: int = int(data["n_tested"])
                n_observed: int = int(data["n_observed"])
                pmids: set[int] = data["pmids"]  # type: ignore[assignment]

                interaction = None
                if (p1_id, p2_id) in interaction_pairs:
                    interaction = interactions.get((p1_id, p2_id))

                if interaction is not None:
                    target_kwargs: dict[str, object] = {"interaction": interaction}
                elif n_observed == 0 and n_tested > min_tests:
                    ni = existing_noninteractions.get((p1_id, p2_id))
                    if ni is None:
                        ni, created_ni = NonInteraction.objects.get_or_create(
                            protein_1_id=p1_id,
                            protein_2_id=p2_id,
                            defaults={"score": 0.0},
                        )
                        if created_ni:
                            noninteractions_created += 1
                            existing_noninteractions[(p1_id, p2_id)] = ni
                    target_kwargs = {"noninteraction": ni}
                else:
                    skipped_no_target += 1
                    continue

                assoc, created = BaitPreyAssociation.objects.get_or_create(
                    **target_kwargs,
                    defaults={
                        "number_of_tests": n_tested,
                        "number_of_observed": n_observed,
                    },
                )
                if created:
                    assocs_created += 1
                else:
                    BaitPreyAssociation.objects.filter(pk=assoc.pk).update(
                        number_of_tests=F("number_of_tests") + n_tested,
                        number_of_observed=F("number_of_observed") + n_observed,
                    )
                    assocs_updated += 1

                pubs = [pub_map[p] for p in pmids if p in pub_map]
                if pubs:
                    assoc.publications.add(*pubs)

        self.stdout.write(
            self.style.SUCCESS(
                f"Done — rows read: {rows_read:,}, "
                f"skipped (no protein): {skipped_missing_protein:,}, "
                f"skipped (observed, no interaction): {skipped_rejected:,}, "
                f"skipped (no target): {skipped_no_target:,}, "
                f"NonInteractions created: {noninteractions_created:,}, "
                f"BaitPreyAssociations created: {assocs_created:,}, "
                f"BaitPreyAssociations updated: {assocs_updated:,}"
            )
        )
