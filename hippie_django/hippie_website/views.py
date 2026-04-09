"""
Views for the HIPPIE protein query interface.

Provides:
  - protein_query_view        : landing page (renders the React shell)
  - protein_query_api         : JSON endpoint consumed by the React table
  - interaction_detail_view   : single interaction evidence page
  - protein_detail_view       : brief protein summary page

All database access goes through the custom managers defined in managers.py:
  - Protein.objects.resolve(identifier)       → ProteinQuerySet
  - Interaction.objects.for_protein(pk)       → InteractionQuerySet
  - Interaction.objects.with_proteins()       → adds select_related + prefetch
  - Interaction.objects.with_full_detail()    → full prefetch for detail page
"""

from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_GET

from .models import Interaction, Protein


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _protein_display(protein: Protein) -> dict:
    """
    Return a compact serialisable dict for a Protein instance.

    Assumes `uniprot_ids` and `entrez_ids` have already been prefetched
    (either by the manager's with_proteins() or an explicit prefetch_related).
    Accessing .all() on a prefetched relation hits the prefetch cache —
    no extra queries.
    """
    uniprot = (
        # prefetch cache is ordered by "-version" when loaded via with_proteins()
        protein.uniprot_ids.all().first()
    )
    entrez = protein.entrez_ids.all().first()
    return {
        "id":         protein.pk,
        "name":       protein.name,
        "uniprot_id": uniprot.uniprot_id if uniprot else "",
        "gene_id":    entrez.gene_id     if entrez  else None,
        "symbol":     entrez.name        if entrez  else protein.name,
    }


# ---------------------------------------------------------------------------
# Landing page
# ---------------------------------------------------------------------------

@require_GET
def protein_query_view(request):
    """Render the React shell. All data is loaded via the JSON API."""
    return render(request, "hippie_website/index.html")


# ---------------------------------------------------------------------------
# JSON API – protein query
# ---------------------------------------------------------------------------

@require_GET
def protein_query_api(request):
    """
    GET /api/query/?q=<identifier>

    Resolves the identifier via Protein.objects.resolve(), then fetches
    all interactions via Interaction.objects.for_protein().with_proteins()
    so that partner identifier mappings are available in a single round-trip.

    Response shape:
    {
        "query_protein": { id, name, uniprot_id, gene_id, symbol },
        "interactions": [
            {
                "id":               <int>,
                "partner":          { id, name, uniprot_id, gene_id, symbol },
                "score":            <float>,
                "source_count":     <int>,
                "experiment_count": <int>,
                "detail_url":       "/interaction/<id>/"
            },
            ...
        ],
        "error": null | "<message>"
    }
    """
    q = request.GET.get("q", "").strip()
    if not q:
        return JsonResponse({
            "error": "No query provided.",
            "interactions": [],
            "query_protein": None,
        })

    # ── Resolve identifier → Protein ──────────────────────────────────
    # resolve() returns a queryset; prefetch IDs so _protein_display()
    # can read them without extra queries.
    proteins = (
        Protein.objects
        .resolve(q)
        .prefetch_related("uniprot_ids", "entrez_ids")
    )

    if not proteins.exists():
        return JsonResponse({
            "error": f"No protein found for '{q}'.",
            "interactions": [],
            "query_protein": None,
        })

    # If the identifier is ambiguous (rare) take the first match.
    protein = proteins.first()

    # ── Fetch interactions ─────────────────────────────────────────────
    # for_protein()  → filters to interactions involving this protein
    # with_proteins() → select_related + prefetch partner ID mappings
    # prefetch_related("sources", "experiments") for the count columns
    interactions_qs = (
        Interaction.objects
        .for_protein(protein.pk)
        .with_proteins()
        .prefetch_related("sources", "experiments")
        .order_by("-score")
    )

    results = []
    for interaction in interactions_qs:
        # Determine which side is the queried protein and which is the partner.
        if interaction.protein_1_id == protein.pk:
            partner = interaction.protein_2
        else:
            partner = interaction.protein_1

        results.append({
            "id":               interaction.pk,
            "partner":          _protein_display(partner),
            "score":            round(interaction.score, 4),
            # .all() on a prefetched M2M hits the cache — no extra queries.
            "source_count":     interaction.sources.all().count(),
            "experiment_count": interaction.experiments.all().count(),
            "detail_url":       reverse(
                "hippie_website:interaction_detail", args=[interaction.pk]
            ),
        })

    return JsonResponse({
        "query_protein": _protein_display(protein),
        "interactions":  results,
        "error":         None,
    })


# ---------------------------------------------------------------------------
# Interaction detail view
# ---------------------------------------------------------------------------

@require_GET
def interaction_detail_view(request, pk: int):
    """
    Show full evidence for a single interaction.

    Uses Interaction.objects.with_full_detail() which chains:
      with_proteins()    → both protein FKs + their UniProt/Entrez IDs
      with_evidence()    → sources, publications, experiments,
                           conserved_species, interaction_types,
                           cross_references (+ source + species)
      with_annotations() → go_terms, mesh_terms
    """
    interaction = get_object_or_404(
        Interaction.objects.with_full_detail(),
        pk=pk,
    )

    # Convenience accessors — all relations are prefetched, no extra queries.
    p1_uniprot = interaction.protein_1.uniprot_ids.all().first()
    p2_uniprot = interaction.protein_2.uniprot_ids.all().first()
    p1_entrez  = interaction.protein_1.entrez_ids.all().first()
    p2_entrez  = interaction.protein_2.entrez_ids.all().first()

    context = {
        "interaction": interaction,
        "p1": {
            "protein":   interaction.protein_1,
            "uniprot_id": p1_uniprot.uniprot_id if p1_uniprot else "",
            "gene_id":    p1_entrez.gene_id     if p1_entrez  else None,
            "symbol":     p1_entrez.name        if p1_entrez  else interaction.protein_1.name,
        },
        "p2": {
            "protein":   interaction.protein_2,
            "uniprot_id": p2_uniprot.uniprot_id if p2_uniprot else "",
            "gene_id":    p2_entrez.gene_id     if p2_entrez  else None,
            "symbol":     p2_entrez.name        if p2_entrez  else interaction.protein_2.name,
        },
        # All prefetched — .all() hits the cache.
        "sources":      interaction.sources.all(),
        "publications": interaction.publications.all(),
        "experiments":  interaction.experiments.all().order_by("-quality_score"),
        "species":      interaction.conserved_species.all(),
    }
    return render(request, "hippie_website/interaction_detail.html", context)


# ---------------------------------------------------------------------------
# Protein detail view
# ---------------------------------------------------------------------------

@require_GET
def protein_detail_view(request, pk: int):
    """
    Brief protein summary — scaffold for future extension.

    Uses resolve() indirectly: the protein is fetched by PK (already
    resolved), with ID mappings prefetched so the template needs no
    extra queries.
    """
    protein = get_object_or_404(
        Protein.objects.prefetch_related("uniprot_ids", "entrez_ids"),
        pk=pk,
    )

    uniprot = protein.uniprot_ids.all().first()
    entrez  = protein.entrez_ids.all().first()

    # Count interactions using the manager for consistency.
    interaction_count = (
        Interaction.objects
        .for_protein(protein.pk)
        .count()
    )

    context = {
        "protein":          protein,
        "uniprot_id":       uniprot.uniprot_id if uniprot else "",
        "gene_id":          entrez.gene_id     if entrez  else None,
        "symbol":           entrez.name        if entrez  else protein.name,
        "interaction_count": interaction_count,
    }
    return render(request, "hippie_website/protein_detail.html", context)