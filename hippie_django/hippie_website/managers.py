"""
Custom managers and querysets for HIPPIE — optimised for a 99% read workload.

Usage in models.py:
    class Protein(models.Model):
        ...
        objects = ProteinManager()

    class Interaction(models.Model):
        ...
        objects = InteractionManager()
"""

from django.db import models
from django.db.models import CharField, Q, Value


# ============================================================================
# Protein
# ============================================================================


class ProteinQuerySet(models.QuerySet):
    """Reusable queryset methods for Protein."""

    # ------------------------------------------------------------------
    # ID resolution  (mirrors the PHP  getProteinDBid / convert* chain)
    # ------------------------------------------------------------------

    def resolve(self, identifier: str) -> "ProteinQuerySet":
        """
        Resolve an arbitrary identifier (UniProt accession, UniProt entry ID,
        Entrez gene ID, or gene symbol) to the matching Protein(s).

        Returns a queryset so it composes with further filters.
        Tries each identifier type in order and returns the first match.
        """
        qs = self.none()

        # 1) Pure digits → Entrez gene ID
        if identifier.isdigit():
            qs = self.filter(gene__entrez_id=int(identifier))
            if qs.exists():
                return qs

        # 2) Contains underscore  (e.g. "BRCA1_HUMAN") → UniProt entry name
        if "_" in identifier:
            qs = self.filter(uniprot_name=identifier)
            if qs.exists():
                return qs

        # 3) Isoform-specific UniProt accession (e.g. "P38398-2")
        #    Returns the parent Protein queryset so callers stay consistent
        from . import models as m  # late import to avoid circularity

        isoform_pk = None
        isoform_uid = None
        if "-" in identifier:
            isoform = (
                m.Isoform.objects.filter(uniprot_accession=identifier)
                .values("protein_ptr_id", "uniprot_accession")
                .first()
            )
            if isoform:
                isoform_pk = isoform["protein_ptr_id"]
                isoform_uid = isoform["uniprot_accession"]
        if isoform_pk is not None:
            qs = self.filter(pk=isoform_pk)
            if isoform_uid is not None:
                qs = qs.annotate(
                    isoform_uniprot_id=Value(isoform_uid, output_field=CharField())
                )
            if qs.exists():
                return qs

        # 4) UniProt accession  (e.g. "P38398")
        qs = self.filter(uniprot_accession=identifier)
        if qs.exists():
            return qs

        # 5) Gene symbol / UniProt entry name
        qs = self.filter(
            Q(gene__entrez_name__iexact=identifier) | Q(uniprot_name__iexact=identifier)
        )
        if qs.exists():
            return qs

        return self.none()

    # ------------------------------------------------------------------
    # Tissue filtering
    # ------------------------------------------------------------------

    def expressed_in(
        self, tissue_ids: list[int], min_rpkm: float | None = None
    ) -> "ProteinQuerySet":
        """Filter to proteins expressed in *any* of the given tissues."""
        if min_rpkm is not None:
            return self.filter(
                gene__tissue_expression__tissue_id__in=tissue_ids,
                gene__tissue_expression__median_rpkm__gte=min_rpkm,
            ).distinct()
        return self.filter(gene__tissue_expression__tissue_id__in=tissue_ids).distinct()


class ProteinManager(models.Manager):
    def get_queryset(self):
        return ProteinQuerySet(self.model, using=self._db)

    def resolve(self, identifier: str):
        return self.get_queryset().resolve(identifier)

    def expressed_in(self, tissue_ids, min_rpkm=None):
        return self.get_queryset().expressed_in(tissue_ids, min_rpkm)


# ============================================================================
# Interaction
# ============================================================================


class InteractionQuerySet(models.QuerySet):
    """Reusable queryset methods for Interaction."""

    # ------------------------------------------------------------------
    # Prefetch bundles  (avoids N+1 on the detail & results pages)
    # ------------------------------------------------------------------

    def with_proteins(self) -> "InteractionQuerySet":
        """
        select_related both protein FKs and their Gene.
        Covers the columns shown on every results table row:
            UniProt accession, Entrez gene ID, gene symbol (for both sides).
        """
        return self.select_related(
            "protein_1",
            "protein_1__gene",
            "protein_2",
            "protein_2__gene",
        )

    def with_evidence(self) -> "InteractionQuerySet":
        """
        Prefetch all evidence needed for the interaction detail page:
        sources, publications, experiments, cross-references,
        and bait-prey detection tests.
        """
        return self.prefetch_related(
            "sources",
            "publications",
            "experiments",
            "interaction_types",
            "cross_references",
            "cross_references__source",
            "cross_references__species",
            "bait_prey",
            "bait_prey__publications",
        )

    def with_full_detail(self) -> "InteractionQuerySet":
        """Combine all prefetches — for the single-interaction detail page."""
        return self.with_proteins().with_evidence()

    # ------------------------------------------------------------------
    # Protein-centric queries  (mirrors getInteractorsWithBlackList)
    # ------------------------------------------------------------------

    def for_protein(self, protein_id: int) -> "InteractionQuerySet":
        """All interactions involving a protein (either side)."""
        return self.filter(Q(protein_1_id=protein_id) | Q(protein_2_id=protein_id))

    def for_proteins(self, protein_ids: list[int]) -> "InteractionQuerySet":
        """All interactions involving any of the given proteins."""
        return self.filter(
            Q(protein_1_id__in=protein_ids) | Q(protein_2_id__in=protein_ids)
        )

    def between_proteins(self, protein_ids: list[int]) -> "InteractionQuerySet":
        """Only interactions where *both* partners are in the set (layer 0)."""
        return self.filter(
            protein_1_id__in=protein_ids,
            protein_2_id__in=protein_ids,
        )

    # ------------------------------------------------------------------
    # Score filtering
    # ------------------------------------------------------------------

    def above_score(self, threshold: float) -> "InteractionQuerySet":
        """Keep only interactions with score >= threshold."""
        return self.filter(score__gte=threshold)

    # ------------------------------------------------------------------
    # Tissue filtering  (both interactors must be expressed)
    # ------------------------------------------------------------------

    def in_tissues(
        self, tissue_ids: list[int], min_rpkm: float | None = None
    ) -> "InteractionQuerySet":
        """
        Keep interactions where *both* proteins are expressed in at least
        one of the given tissues.
        """
        from . import models as m

        gt_qs = m.GeneTissue.objects.filter(tissue_id__in=tissue_ids)
        if min_rpkm is not None:
            gt_qs = gt_qs.filter(median_rpkm__gte=min_rpkm)
        expressed = gt_qs.values_list("gene__proteins__id", flat=True)

        return self.filter(
            protein_1_id__in=expressed,
            protein_2_id__in=expressed,
        )


class InteractionManager(models.Manager):
    def get_queryset(self):
        return InteractionQuerySet(self.model, using=self._db)

    def with_proteins(self):
        return self.get_queryset().with_proteins()

    def with_full_detail(self):
        return self.get_queryset().with_full_detail()

    def for_protein(self, protein_id):
        return self.get_queryset().for_protein(protein_id)

    def for_proteins(self, protein_ids):
        return self.get_queryset().for_proteins(protein_ids)
