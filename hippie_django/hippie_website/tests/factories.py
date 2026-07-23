"""
Shared fixtures and factories for the HIPPIE test package.

Extracted from the former monolithic ``tests.py`` so the ``make_*`` factories,
the denormalisation-refresh helpers, and the base :class:`HippieTestCase` live
in one place imported by every ``test_*`` module in this package.
"""

from io import StringIO

from django.core.management import call_command
from django.test import Client, TestCase

from ..models import (
    ExperimentType,
    Gene,
    Interaction,
    NonInteraction,
    Protein,
    Source,
)


# ---------------------------------------------------------------------------
# Fixtures — wiederverwendbare Testdaten
# ---------------------------------------------------------------------------


def make_protein(name, uniprot_name=None, gene_id=None, accession=None):
    """Erstellt ein Protein mit optionalen Identifier-Mappings."""
    if gene_id is not None:
        gene, _ = Gene.objects.get_or_create(
            entrez_id=gene_id, defaults={"entrez_name": name}
        )
    else:
        gene, _ = Gene.objects.get_or_create(entrez_id=0, defaults={"entrez_name": ""})
    return Protein.objects.create(
        gene=gene,
        uniprot_name=uniprot_name or (name if gene_id is None else ""),
        uniprot_accession=accession if accession is not None else f"TEST_{name}",
        # Fixtures model well-known Swiss-Prot proteins → reviewed. The model
        # default is False (proteins start unreviewed until update_review_status
        # flips them against UniProt's reviewed list); tests set True explicitly.
        is_reviewed=True,
    )


def make_interaction(p1, p2, score=0.8):
    """Erstellt eine Interaction in kanonischer Reihenfolge."""
    a, b = (p1, p2) if p1.pk <= p2.pk else (p2, p1)
    return Interaction.objects.create(protein_1=a, protein_2=b, score=score)


def make_noninteraction(p1, p2, score=0.3):
    """Erstellt eine NonInteraction in kanonischer Reihenfolge."""
    a, b = (p1, p2) if p1.pk <= p2.pk else (p2, p1)
    return NonInteraction.objects.create(protein_1=a, protein_2=b, score=score)


def recompute_stats():
    """Refresh the denormalised Protein.degree / avg_score columns (test infra)."""
    call_command("recompute_protein_stats", stdout=StringIO())


def recompute_flags():
    """Refresh Interaction.involves_isoform / n_sources / n_experiments (test infra)."""
    call_command("recompute_interaction_flags", stdout=StringIO())


# ---------------------------------------------------------------------------
# Basis-Testklasse mit gemeinsamen Fixtures
# ---------------------------------------------------------------------------


class HippieTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.brca1 = make_protein(
            "BRCA1", uniprot_name="BRCA1_HUMAN", gene_id=672, accession="P38398"
        )
        cls.tp53 = make_protein(
            "TP53", uniprot_name="P53_HUMAN", gene_id=7157, accession="P04637"
        )
        cls.egfr = make_protein(
            "EGFR", uniprot_name="EGFR_HUMAN", gene_id=1956, accession="P00533"
        )
        cls.ix = make_interaction(cls.brca1, cls.tp53, score=0.85)
        cls.src = Source.objects.create(
            name="BioGRID", url="https://thebiogrid.org/", n_connected_interactions=1
        )
        cls.exp = ExperimentType.objects.create(
            name="Two-hybrid", psi_mi_code="MI:0018", quality_score=5.0
        )
        cls.ix.sources.add(cls.src)
        cls.ix.experiments.add(cls.exp)
        # Browse reads denormalised degree/avg_score columns; populate them so
        # min_degree / min_score filter tests exercise real values.
        recompute_stats()
        cls.client = Client()

    def setUp(self):
        # Browse totals are memoised in the (process-global) cache; clear it
        # between tests so a cached count from one test can't leak into another.
        from django.core.cache import cache

        cache.clear()
