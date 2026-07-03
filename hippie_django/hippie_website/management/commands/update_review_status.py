"""
Refresh ``Protein.is_reviewed`` from UniProt's reviewed (Swiss-Prot) accession list.

Streams the full list of reviewed accessions from UniProt's REST API and marks
matching canonical proteins as reviewed. Isoform accessions (e.g. "P12345-2")
are not looked up individually — UniProt's reviewed list only carries
canonical accessions — so an isoform's `is_reviewed` mirrors its
`general_protein`.
"""

import urllib.request

from django.core.management.base import BaseCommand

from hippie_website.models import Isoform, Protein

UNIPROT_REVIEWED_STREAM_URL = (
    "https://rest.uniprot.org/uniprotkb/stream?query=reviewed:true&format=list"
)


def _stream_reviewed_accessions(url: str = UNIPROT_REVIEWED_STREAM_URL) -> set[str]:
    request = urllib.request.Request(
        url, headers={"User-Agent": "hippie-review-status/1.0"}
    )
    accessions: set[str] = set()
    with urllib.request.urlopen(request, timeout=600) as response:
        for line in response:
            accession = line.decode("utf-8").strip()
            if accession:
                accessions.add(accession)
    return accessions


def update_review_status(stdout, batch_size: int = 2000) -> None:
    stdout.write("Streaming reviewed accessions from UniProt...")
    reviewed_accessions = _stream_reviewed_accessions()
    stdout.write(f"  {len(reviewed_accessions):,} reviewed accessions")

    isoform_general_map: dict[int, int] = dict(
        Isoform.objects.values_list("pk", "general_protein_id")
    )

    proteins = list(
        Protein.objects.all().only("pk", "uniprot_accession", "is_reviewed")
    )

    canonical_reviewed: dict[int, bool] = {}
    to_update: list[Protein] = []
    changed = 0

    for protein in proteins:
        if protein.pk in isoform_general_map:
            continue
        is_reviewed = protein.uniprot_accession in reviewed_accessions
        canonical_reviewed[protein.pk] = is_reviewed
        if protein.is_reviewed != is_reviewed:
            protein.is_reviewed = is_reviewed
            to_update.append(protein)
            changed += 1

    for protein in proteins:
        general_pk = isoform_general_map.get(protein.pk)
        if general_pk is None:
            continue
        is_reviewed = canonical_reviewed.get(general_pk, False)
        if protein.is_reviewed != is_reviewed:
            protein.is_reviewed = is_reviewed
            to_update.append(protein)
            changed += 1

    for i in range(0, len(to_update), batch_size):
        Protein.objects.bulk_update(to_update[i : i + batch_size], ["is_reviewed"])

    stdout.write(
        f"Updated is_reviewed for {len(proteins)} proteins ({changed} changed)."
    )


class Command(BaseCommand):
    help = "Update Protein.is_reviewed from UniProt's reviewed accession list."

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch-size",
            type=int,
            default=2000,
            help="Rows per bulk_update batch (default 2000).",
        )

    def handle(self, *args, **options):
        update_review_status(self.stdout, batch_size=options["batch_size"])
