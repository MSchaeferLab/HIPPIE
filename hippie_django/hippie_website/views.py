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

import json

from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_GET
from django.views.generic.edit import FormView

from .forms import NetworkQueryForm
from .models import Interaction, Protein, Tissue


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
# Network query
# ---------------------------------------------------------------------------

# Choices passed to the template for the filter form
_DIRECTIONALITY_CHOICES = [
    ("any",      "Any"),
    ("directed", "Directed only"),
    ("undirected","Undirected only"),
]

_EFFECT_CHOICES = [
    ("activation",  "Activation"),
    ("inhibition",  "Inhibition"),
    ("binding",     "Binding"),
    ("reaction",    "Reaction"),
    ("other",       "Other / unknown"),
]


class NetworkQueryView(FormView):
    """
    GET  → render the blank NetworkQueryForm.
    POST → validate, run the query, re-render with results.

    _run_network_query() is also called by the JSON API endpoint so the
    query logic lives in one place.
    """

    template_name = "hippie_website/network_query.html"
    form_class    = NetworkQueryForm

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.setdefault("network_result", None)
        return ctx

    def form_valid(self, form):
        cd = form.cleaned_data

        # Merge textarea + uploaded file into one newline-separated string
        raw_proteins = cd.get("proteins", "") or ""
        uploaded = cd.get("proteins_file")
        if uploaded:
            raw_proteins += "\n" + uploaded.read().decode("utf-8", errors="replace")

        # layer_0-only → between_proteins (both partners must be seeds).
        # layer_1 (or neither box checked) → for_proteins (all edges touching seeds).
        layer_0 = cd.get("layer_0", False)
        layer_1 = cd.get("layer_1", False)
        expand = "none" if (layer_0 and not layer_1) else "first_shell"

        params = {
            "proteins":      raw_proteins,
            "expand":        expand,
            "score_min":     cd.get("score_min") or 0.0,
            "directionality": {
                "none":         "any",
                "kegg":         "directed",
                "unweighted_sp":"any",
                "weighted_sp":  "any",
            }.get(cd.get("direction", "none"), "any"),
            "effect_type": {
                "predicted": ["activation", "inhibition"],
                "kegg":      ["activation", "inhibition"],
            }.get(cd.get("effect", "none"), []),
            "tissues":   [cd["tissue"].name] if cd.get("tissue") else [],
            "go_terms":  cd.get("go_terms",  ""),
            "mesh_terms":cd.get("mesh_terms", ""),
        }
        result = _run_network_query(params)
        output_type = cd.get("output_type", "browser_vis")
        return self.render_to_response(self.get_context_data(
            form=form,
            network_result=result,
            output_type=output_type,
            cy_edges_json=json.dumps(result["interactions"]) if output_type == "browser_vis" else "[]",
            cy_seeds_json=json.dumps(result.get("seed_proteins", [])) if output_type == "browser_vis" else "[]",
        ))


network_query_view = NetworkQueryView.as_view()


@require_GET
def network_query_api(request):
    """
    GET /api/network/?proteins=...&score_min=...&...

    Returns the same shape as protein_query_api but for a set of proteins:
    {
        "node_count":   <int>,
        "edge_count":   <int>,
        "interactions": [
            { id, protein_a, protein_b, score, source_count, experiment_count }
        ],
        "error": null | "<message>"
    }
    """
    result = _run_network_query(request.GET)
    if result.get("error"):
        return JsonResponse(result, status=400)
    return JsonResponse(result)


def _get_tissue_list():
    """Return all Tissue names, sorted, for the filter form checkboxes."""
    return list(Tissue.objects.values_list("name", flat=True).order_by("name"))


def _run_network_query(params) -> dict:
    """
    Execute the network query from GET parameters.

    params keys:
        proteins        — newline-separated identifiers (gene symbols, UniProt, Entrez)
        expand          — "none" | "first_shell" | "second_shell"
        score_min       — float 0–1
        directionality  — "any" | "directed" | "undirected"
        effect_type     — list of "activation" | "inhibition"
        tissue_mode     — "any" | "both" | "one"  (unused: we always require both)
        tissues         — list of tissue names
        go_terms        — comma-separated GO IDs
        mesh_terms      — comma-separated MeSH numbers
    """
    # -- 1. Resolve seed proteins -----------------------------------------
    raw = params.get("proteins", "")
    # Accept newline- or space-separated identifiers from both the textarea
    # and the GET query string
    seeds = raw.split() if raw else []
    if not seeds:
        return {
            "node_count": 0, "edge_count": 0,
            "interactions": [], "error": "No seed proteins provided.",
        }

    protein_ids: list[int] = []
    unresolved: list[str] = []
    seen: set[int] = set()
    for ident in seeds:
        pk = (
            Protein.objects.resolve(ident)
            .values_list("pk", flat=True)
            .first()
        )
        if pk is not None and pk not in seen:
            protein_ids.append(pk)
            seen.add(pk)
        elif pk is None:
            unresolved.append(ident)

    if not protein_ids:
        return {
            "node_count": 0, "edge_count": 0,
            "interactions": [],
            "error": f"None of the identifiers could be resolved: {', '.join(unresolved)}",
        }

    # -- 2. Expansion (layer) ---------------------------------------------
    expand = params.get("expand", "none")
    layer = 0 if expand == "none" else 1   # second_shell treated as layer 1 for now

    # -- 3. Score ---------------------------------------------------------
    try:
        score_min = float(params.get("score_min", 0))
    except (TypeError, ValueError):
        score_min = 0.0

    # -- 4. Tissue filter -------------------------------------------------
    # params may be a QueryDict (from the API) or a plain dict (from form_valid)
    if hasattr(params, "getlist"):
        tissue_names = params.getlist("tissues")
    else:
        tissue_names = params.get("tissues") or []
    tissue_ids: list[int] | None = None
    if tissue_names:
        tissue_ids = list(
            Tissue.objects.filter(name__in=tissue_names).values_list("pk", flat=True)
        )

    # -- 5. Core queryset -------------------------------------------------
    qs = Interaction.objects.network_query(
        protein_ids,
        layer=layer,
        score_threshold=score_min,
        tissue_ids=tissue_ids,
    ).prefetch_related(
        "sources",
        "experiments",
        "protein_1__uniprot_ids",
        "protein_1__entrez_ids",
        "protein_2__uniprot_ids",
        "protein_2__entrez_ids",
    )

    # -- 6. Directionality filter -----------------------------------------
    directionality = params.get("directionality", "any")
    if directionality == "directed":
        qs = qs.exclude(kegg_direction__isnull=True)
    elif directionality == "undirected":
        qs = qs.filter(kegg_direction__isnull=True)

    # -- 7. Effect type filter --------------------------------------------
    if hasattr(params, "getlist"):
        effect_types = params.getlist("effect_type")
    else:
        effect_types = params.get("effect_type") or []
    if effect_types:
        effect_q = Q()
        if "activation" in effect_types:
            effect_q |= Q(effect_type=Interaction.EffectType.ACTIVATION)
        if "inhibition" in effect_types:
            effect_q |= Q(effect_type=Interaction.EffectType.INHIBITION)
        qs = qs.filter(effect_q)

    # -- 8. GO / MeSH annotation filters ----------------------------------
    go_raw = params.get("go_terms", "")
    go_ids = [v.strip() for v in go_raw.split(",") if v.strip()]
    if go_ids:
        qs = qs.filter(go_terms__id__in=go_ids).distinct()

    mesh_raw = params.get("mesh_terms", "")
    mesh_ids = [v.strip() for v in mesh_raw.split(",") if v.strip()]
    if mesh_ids:
        qs = qs.filter(mesh_terms__number__in=mesh_ids).distinct()

    # -- 9. Serialise -----------------------------------------------------
    interactions = []
    node_ids: set[int] = set()
    for ix in qs:
        node_ids.add(ix.protein_1_id)
        node_ids.add(ix.protein_2_id)
        p1_uniprot = ix.protein_1.uniprot_ids.all().first()
        p1_entrez  = ix.protein_1.entrez_ids.all().first()
        p2_uniprot = ix.protein_2.uniprot_ids.all().first()
        p2_entrez  = ix.protein_2.entrez_ids.all().first()
        interactions.append({
            "interaction_id":   ix.pk,
            "protein_a":        ix.protein_1.name,
            "uniprot_a":        p1_uniprot.uniprot_id if p1_uniprot else "",
            "entrez_a":         p1_entrez.gene_id     if p1_entrez  else "",
            "gene_name_a":      p1_entrez.name        if p1_entrez  else ix.protein_1.name,
            "protein_b":        ix.protein_2.name,
            "uniprot_b":        p2_uniprot.uniprot_id if p2_uniprot else "",
            "entrez_b":         p2_entrez.gene_id     if p2_entrez  else "",
            "gene_name_b":      p2_entrez.name        if p2_entrez  else ix.protein_2.name,
            "score":            round(ix.score, 4),
            "source_count":     ix.sources.all().count(),
            "experiment_count": ix.experiments.all().count(),
            "uploaded_interaction": ix.protein_1_id in seen and ix.protein_2_id in seen,
            "kegg_direction":   ix.kegg_direction,
            "effect_type":      ix.effect_type,
        })

    seed_names = set(
        Protein.objects.filter(pk__in=protein_ids).values_list("name", flat=True)
    )

    return {
        "node_count":    len(node_ids),
        "edge_count":    len(interactions),
        "interactions":  interactions,
        "seed_proteins": list(seed_names),
        "unresolved":    unresolved,
        "error":         None,
    }


# ---------------------------------------------------------------------------
# Browse
# ---------------------------------------------------------------------------

@require_GET
def browse_view(request):
    """Browse all interactions — stub."""
    return render(request, "hippie_website/browse.html", {})


# ---------------------------------------------------------------------------
# Screen annotation
# ---------------------------------------------------------------------------

@require_GET
def screen_annotation_view(request):
    """Screen annotation tool — stub."""
    return render(request, "hippie_website/screen_annotation.html", {})


# ---------------------------------------------------------------------------
# Utility pages
# ---------------------------------------------------------------------------

@require_GET
def download_view(request):
    """Database download page — stub."""
    return render(request, "hippie_website/download.html", {})


@require_GET
def information_view(request):
    """Documentation / information page — stub."""
    return render(request, "hippie_website/information.html", {})

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


