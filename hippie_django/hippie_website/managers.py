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
from django.db.models import Avg, Count, F, Prefetch, Q, Subquery, OuterRef, Sum, Value
from django.db.models.expressions import RawSQL
from django.db.models.functions import NullIf


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
        Resolve an arbitrary identifier (UniProt ID, UniProt accession,
        Entrez gene ID, or gene symbol) to the matching Protein(s).

        Returns a queryset so it composes with further filters.
        Tries each identifier type in order and returns the first match.
        """
        qs = self.none()

        # 1) Pure digits → Entrez gene ID
        if identifier.isdigit():
            qs = self.filter(entrez_ids__gene_id=int(identifier))
            if qs.exists():
                return qs

        # 2) Contains underscore  (e.g. "BRCA1_HUMAN") → UniProt entry ID
        if "_" in identifier:
            qs = self.filter(uniprot_ids__uniprot_id=identifier)
            if qs.exists():
                return qs

        # 3) UniProt accession  (e.g. "P38398")
        from . import models as m  # late import to avoid circularity

        uniprot_id = (
            m.UniProtAccession.objects.filter(accession=identifier)
            .values_list("uniprot_id", flat=True)
            .first()
        )
        if uniprot_id:
            qs = self.filter(uniprot_ids__uniprot_id=uniprot_id)
            if qs.exists():
                return qs

        # 4) Gene symbol
        qs = self.filter(entrez_ids__name__iexact=identifier)
        if qs.exists():
            return qs

        return self.none()

    # ------------------------------------------------------------------
    # Annotations used by the "browse" page
    # ------------------------------------------------------------------

    def with_browse_annotations(self) -> "ProteinQuerySet":
        """
        Annotate each protein with its degree (interaction count) and
        average interaction score — the two numbers shown on browse.php.

        Also select_related the first UniProt ID and Entrez mapping so
        the template can render them without extra queries.
        """
        return (
            self.prefetch_related(
                Prefetch("uniprot_ids", queryset=self._uniprot_qs()),
                Prefetch("entrez_ids"),
            )
            .annotate(
                degree=Count("interactions_as_1", distinct=True) + Count("interactions_as_2", distinct=True),
                avg_score=RawSQL(
                    """(SELECT AVG(score) FROM interaction
                       WHERE protein_1_id = protein.id OR protein_2_id = protein.id)""",
                    []
                ),
            )
        )

    # ------------------------------------------------------------------
    # Tissue filtering
    # ------------------------------------------------------------------

    def expressed_in(self, tissue_ids: list[int]) -> "ProteinQuerySet":
        """Filter to proteins expressed in *any* of the given tissues."""
        return self.filter(tissue_expression__tissue_id__in=tissue_ids).distinct()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _uniprot_qs():
        """Latest-version UniProt mapping (ORDER BY version DESC)."""
        from . import models as m

        return m.ProteinUniProt.objects.order_by("-version")


class ProteinManager(models.Manager):
    def get_queryset(self):
        return ProteinQuerySet(self.model, using=self._db)

    def resolve(self, identifier: str):
        return self.get_queryset().resolve(identifier)

    def with_browse_annotations(self):
        return self.get_queryset().with_browse_annotations()

    def expressed_in(self, tissue_ids):
        return self.get_queryset().expressed_in(tissue_ids)


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
        select_related both protein FKs + prefetch their identifier
        mappings.  Covers the columns shown on every results table row:
            UniProt ID, Entrez gene ID, gene symbol (for both sides).
        """
        from . import models as m

        return self.select_related("protein_1", "protein_2").prefetch_related(
            Prefetch(
                "protein_1__uniprot_ids",
                queryset=m.ProteinUniProt.objects.order_by("-version"),
            ),
            Prefetch("protein_1__entrez_ids"),
            Prefetch(
                "protein_2__uniprot_ids",
                queryset=m.ProteinUniProt.objects.order_by("-version"),
            ),
            Prefetch("protein_2__entrez_ids"),
        )

    def with_evidence(self) -> "InteractionQuerySet":
        """
        Prefetch all evidence needed for the interaction detail page:
        sources, publications, experiments, species, cross-references.
        """
        return self.prefetch_related(
            "sources",
            "publications",
            "experiments",
            "conserved_species",
            "interaction_types",
            "cross_references",
            "cross_references__source",
            "cross_references__species",
        )

    def with_annotations(self) -> "InteractionQuerySet":
        """Prefetch GO and MeSH annotations (needed for functional filtering)."""
        return self.prefetch_related(
            "go_terms",
            "mesh_terms",
        )

    def with_full_detail(self) -> "InteractionQuerySet":
        """Combine all prefetches — for the single-interaction detail page."""
        return self.with_proteins().with_evidence().with_annotations()

    # ------------------------------------------------------------------
    # Protein-centric queries  (mirrors getInteractorsWithBlackList)
    # ------------------------------------------------------------------

    def for_protein(self, protein_id: int) -> "InteractionQuerySet":
        """All interactions involving a protein (either side)."""
        return self.filter(
            Q(protein_1_id=protein_id) | Q(protein_2_id=protein_id)
        )

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

    def in_tissues(self, tissue_ids: list[int]) -> "InteractionQuerySet":
        """
        Keep interactions where *both* proteins are expressed in at least
        one of the given tissues.
        """
        from . import models as m

        expressed = m.ProteinTissue.objects.filter(
            tissue_id__in=tissue_ids
        ).values_list("protein_id", flat=True)

        return self.filter(
            protein_1_id__in=expressed,
            protein_2_id__in=expressed,
        )

    # ------------------------------------------------------------------
    # Interaction type filtering
    # ------------------------------------------------------------------

    def of_types(self, type_ids: list[int]) -> "InteractionQuerySet":
        """Filter by PSI-MI interaction type (association, physical, etc.)."""
        return self.filter(interaction_types__id__in=type_ids).distinct()

    # ------------------------------------------------------------------
    # Directed / effect helpers
    # ------------------------------------------------------------------

    def with_kegg_direction(self) -> "InteractionQuerySet":
        """Annotate with kegg_direction (already inlined on the model)."""
        return self.exclude(kegg_direction__isnull=True)

    def with_effect(self, source: int | None = None) -> "InteractionQuerySet":
        """Filter to interactions that have an effect annotation."""
        qs = self.exclude(effect_type__isnull=True)
        if source is not None:
            qs = qs.filter(effect_source=source)
        return qs

    # ------------------------------------------------------------------
    # Convenience: fully-loaded results page query
    # ------------------------------------------------------------------

    def network_query(
        self,
        protein_ids: list[int],
        *,
        layer: int = 1,
        score_threshold: float = 0.0,
        tissue_ids: list[int] | None = None,
        type_ids: list[int] | None = None,
        load_annotations: bool = False,
    ) -> "InteractionQuerySet":
        """
        One-call equivalent of the PHP fast_query_tissue.php logic.

        Parameters
        ----------
        protein_ids : list of internal Protein PKs
        layer : 0 = within set only, 1 = set vs. HIPPIE
        score_threshold : minimum confidence score
        tissue_ids : if given, both partners must be expressed
        type_ids : PSI-MI interaction type filter
        load_annotations : prefetch GO / MeSH terms (slower)
        """
        if layer == 0:
            qs = self.between_proteins(protein_ids)
        else:
            qs = self.for_proteins(protein_ids)

        if score_threshold > 0:
            qs = qs.above_score(score_threshold)

        if tissue_ids:
            qs = qs.in_tissues(tissue_ids)

        if type_ids:
            qs = qs.of_types(type_ids)

        qs = qs.with_proteins().order_by("-score")

        if load_annotations:
            qs = qs.with_annotations()

        return qs


class InteractionManager(models.Manager):
    def get_queryset(self):
        return InteractionQuerySet(self.model, using=self._db)

    # Expose queryset methods on the manager for convenience
    def with_proteins(self):
        return self.get_queryset().with_proteins()

    def with_full_detail(self):
        return self.get_queryset().with_full_detail()

    def for_protein(self, protein_id):
        return self.get_queryset().for_protein(protein_id)

    def for_proteins(self, protein_ids):
        return self.get_queryset().for_proteins(protein_ids)

    def network_query(self, protein_ids, **kwargs):
        return self.get_queryset().network_query(protein_ids, **kwargs)