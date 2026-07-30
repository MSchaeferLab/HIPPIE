"""Detail-page services: the full evidence behind one (non-)interaction.

Extracted from ``views.py``. Two shapes come out of the same query:

* ``interaction_detail_context`` / ``noninteraction_detail_context`` build the
  **template** context — model instances the detail templates walk directly.
* ``interaction_detail_payload`` builds a **JSON-serializable** payload for the
  MCP tool: the same evidence flattened to primitives, with the PMIDs and
  per-source links an agent needs in order to cite anything it reports.

Both go through ``Interaction.objects.with_full_detail()``, so the evidence is
prefetched once and neither shape issues per-row queries.
"""

from django.conf import settings
from django.shortcuts import get_object_or_404
from django.urls import reverse

from ..models import Interaction, Isoform, NonInteraction, OrthologInteraction, Protein


def protein_detail_ctx(protein: Protein) -> dict:
    """Interactor context dict for the detail templates (protein_pair_base.html):
    the raw protein plus its display accession, Entrez id, and gene symbol."""
    return {
        "protein": protein,
        "uniprot_id": protein.uniprot_accession,
        "gene_id": protein.gene.entrez_id or None,
        "symbol": protein.gene.entrez_name or protein.uniprot_name,
    }


def digger_ctx(p1: Protein, p2: Protein) -> dict:
    """DIGGER cross-links for the "Further information" card, shared by the
    interaction and non-interaction detail pages.

    One extra query resolves which endpoints are isoforms (and loads their
    ENST/ENSP); canonical proteins fall back to the already-``select_related``
    ``gene``. See ``digger_links.py`` for the URL rules.
    """
    from ..digger_links import _first, interaction_digger, protein_digger_url

    isos = {
        i.pk: i
        for i in Isoform.objects.select_related("gene").filter(pk__in=[p1.pk, p2.pk])
    }

    def _one(p: Protein) -> dict:
        iso = isos.get(p.pk)
        if iso is not None:
            return {
                "is_isoform": True,
                "is_canonical": iso.is_canonical,
                "url": protein_digger_url(
                    is_isoform=True,
                    ensg=iso.gene.ensg,
                    enst=iso.enst,
                    ensp=iso.ensp,
                ),
            }
        return {
            "is_isoform": False,
            "is_canonical": False,
            "url": protein_digger_url(
                is_isoform=False, ensg=p.gene.ensg, enst=[], ensp=[]
            ),
        }

    def _transcript_with_fallback(i: Isoform) -> str:
        """Return the first ENST if present, else the first ENSP, else empty string. Used for DIGGER links."""
        return _first(i.enst) or _first(i.ensp) or ""

    p1_iso = p1.pk in isos
    p2_iso = p2.pk in isos
    g1_ensg = isos[p1.pk].gene.ensg if p1_iso else p1.gene.ensg
    g2_ensg = isos[p2.pk].gene.ensg if p2_iso else p2.gene.ensg

    return {
        "p1": _one(p1),
        "p2": _one(p2),
        "interaction": interaction_digger(
            p1_is_isoform=p1_iso,
            p2_is_isoform=p2_iso,
            p1_enst_p=_transcript_with_fallback(isos[p1.pk]) if p1_iso else "",
            p2_enst_p=_transcript_with_fallback(isos[p2.pk]) if p2_iso else "",
            g1_ensg=g1_ensg,
            g2_ensg=g2_ensg,
            handoff_secret=settings.HIPPIE_HANDOFF_SECRET,
        ),
    }


# ---------------------------------------------------------------------------
# Template contexts
# ---------------------------------------------------------------------------


def interaction_detail_context(pk: int) -> dict:
    """Template context for one interaction's evidence page.

    Uses Interaction.objects.with_full_detail() which chains:
      with_proteins()    → both protein FKs + their UniProt/Entrez IDs
      with_evidence()    → sources, publications, experiments,
                           interaction_types,
                           cross_references (+ source + species)
    Conserved species are resolved via OrthologInteraction on the gene pair.

    Raises Http404 when the interaction does not exist.
    """
    interaction = get_object_or_404(
        Interaction.objects.with_full_detail(),
        pk=pk,
    )

    # Compute bait-prey detection stats from prefetched data (no extra queries).
    bait_prey_total_tested = sum(
        assoc.number_of_tests for assoc in interaction.bait_prey.all()
    )
    bait_prey_times_observed = sum(
        assoc.number_of_observed for assoc in interaction.bait_prey.all()
    )

    p1 = interaction.protein_1
    p2 = interaction.protein_2

    g1, g2 = p1.gene, p2.gene
    lo_gene, hi_gene = (g1, g2) if g1.pk <= g2.pk else (g2, g1)
    ortholog = (
        OrthologInteraction.objects.filter(gene_1=lo_gene, gene_2=hi_gene)
        .prefetch_related("ortholog_species")
        .first()
    )
    conserved_species = ortholog.ortholog_species.all() if ortholog else []

    # Annotate each source with a per-pair "all evidence" link where one is
    # known (e.g. IntAct pairwise search); None otherwise. See source_links.py.
    from ..source_links import pair_search_url

    sources = list(interaction.sources.all())
    for source in sources:
        source.pair_url = pair_search_url(
            source.name, p1.uniprot_accession, p2.uniprot_accession
        )

    return {
        "interaction": interaction,
        "p1": protein_detail_ctx(p1),
        "p2": protein_detail_ctx(p2),
        # All prefetched — .all() hits the cache.
        "sources": sources,
        "publications": interaction.publications.all(),
        "experiments": interaction.experiments.all().order_by("-quality_score"),
        "species": conserved_species,
        # Bait-prey detection stats.
        "bait_prey_total_tested": bait_prey_total_tested,
        "bait_prey_times_observed": bait_prey_times_observed,
        # Shared with protein_pair_base.html
        "pair_score": interaction.score,
        "pair_label": "Interaction Evidence",
        "is_noninteraction": False,
        "digger": digger_ctx(p1, p2),
    }


def noninteraction_detail_context(pk: int) -> dict:
    """Template context for one non-interaction's (Negatome) evidence page.

    Raises Http404 when the non-interaction does not exist.
    """
    noninteraction = get_object_or_404(
        NonInteraction.objects.select_related(
            "protein_1", "protein_1__gene", "protein_2", "protein_2__gene"
        ).prefetch_related(
            "bait_prey",
        ),
        pk=pk,
    )

    bait_prey_total_tested = sum(
        assoc.number_of_tests for assoc in noninteraction.bait_prey.all()
    )
    bait_prey_times_observed = sum(
        assoc.number_of_observed for assoc in noninteraction.bait_prey.all()
    )

    p1 = noninteraction.protein_1
    p2 = noninteraction.protein_2
    return {
        "noninteraction": noninteraction,
        "p1": protein_detail_ctx(p1),
        "p2": protein_detail_ctx(p2),
        "bait_prey_total_tested": bait_prey_total_tested,
        "bait_prey_times_observed": bait_prey_times_observed,
        # Shared with protein_pair_base.html
        "pair_score": noninteraction.score,
        "pair_label": "Non-Interaction Evidence",
        "is_noninteraction": True,
        "digger": digger_ctx(p1, p2),
    }


# ---------------------------------------------------------------------------
# Serializable payload (MCP)
# ---------------------------------------------------------------------------


def _protein_payload(protein: Protein) -> dict:
    return {
        "symbol": protein.gene.entrez_name or protein.uniprot_name,
        "uniprot_id": protein.uniprot_accession,
        "uniprot_name": protein.uniprot_name,
        "entrez_id": protein.gene.entrez_id or None,
        "is_reviewed": protein.is_reviewed,
    }


def interaction_detail_payload(pk: int) -> dict:
    """Flatten one interaction's evidence to JSON-serializable primitives.

    Same underlying query as the template context, reshaped for a caller that
    cannot walk model instances. Everything an agent needs to attribute a claim
    is here: the PMIDs behind the interaction, which databases report it (with a
    per-pair deep link where the source supports one), which experimental
    methods were used with their PSI-MI codes, and the bait-prey detection
    counts.

    Raises Http404 when the interaction does not exist.
    """
    interaction = get_object_or_404(Interaction.objects.with_full_detail(), pk=pk)

    p1 = interaction.protein_1
    p2 = interaction.protein_2

    from ..source_links import pair_search_url

    sources = [
        {
            "name": s.name,
            "url": s.url or None,
            "pair_url": pair_search_url(
                s.name, p1.uniprot_accession, p2.uniprot_accession
            ),
        }
        for s in interaction.sources.all()
    ]

    experiments = [
        {
            "name": e.name,
            "psi_mi_code": e.psi_mi_code or None,
            "quality_score": e.quality_score,
        }
        for e in interaction.experiments.all().order_by("-quality_score")
    ]

    return {
        "interaction_id": interaction.pk,
        "score": round(interaction.score, 4),
        "protein_a": _protein_payload(p1),
        "protein_b": _protein_payload(p2),
        "involves_isoform": interaction.involves_isoform,
        "sources": sources,
        "n_sources": len(sources),
        "experiments": experiments,
        "n_experiments": len(experiments),
        "interaction_types": [
            {"name": t.name, "psi_mi_code": t.psi_mi_code or None}
            for t in interaction.interaction_types.all()
        ],
        "publications": [
            {"pmid": pub.pmid, "url": f"https://pubmed.ncbi.nlm.nih.gov/{pub.pmid}/"}
            for pub in interaction.publications.all()
        ],
        "bait_prey_total_tested": sum(
            assoc.number_of_tests for assoc in interaction.bait_prey.all()
        ),
        "bait_prey_times_observed": sum(
            assoc.number_of_observed for assoc in interaction.bait_prey.all()
        ),
        "detail_url": reverse(
            "hippie_website:interaction_detail", args=[interaction.pk]
        ),
    }
