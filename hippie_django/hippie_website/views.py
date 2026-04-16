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
from django.views.decorators.http import require_GET, require_POST
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

def _protein_ids_from_raw(raw: str) -> tuple[list[int], list[str]]:
    """
    Resolve a whitespace-separated string of identifiers to Protein PKs.
    Returns (resolved_pks, unresolved_identifiers).
    """
    protein_ids: list[int] = []
    unresolved:  list[str] = []
    seen: set[int] = set()
    for ident in raw.split():
        pk = Protein.objects.resolve(ident)
        if pk is not None:
            pk = pk.values_list("pk", flat=True).first()

        if pk is not None and pk not in seen:
            protein_ids.append(pk)
            seen.add(pk)
        elif pk is None:
            unresolved.append(ident)
    return protein_ids, unresolved




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
    proteins = Protein.objects.resolve(q).prefetch_related("uniprot_ids", "entrez_ids")

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
        partner = interaction.protein_2 if interaction.protein_1_id == protein.pk else interaction.protein_1


        results.append({
            "id":               interaction.pk,
            "partner":          _protein_display(partner),
            "score":            round(interaction.score, 4),
            # .all() on a prefetched M2M hits the cache — no extra queries.
            "source_count":     interaction.sources.all().count(),
            "experiment_count": interaction.experiments.all().count(),
            "detail_url":       reverse("hippie_website:interaction_detail", args=[interaction.pk]),
        })

    return JsonResponse({
        "query_protein": _protein_display(protein),
        "interactions":  results,
        "error":         None,
    })

# ---------------------------------------------------------------------------
# Interaction query
# ---------------------------------------------------------------------------

MAX_PAIRS   = 5_000 # hard limit enforced server-side and client-site
BATCH_LIMIT = 200 # max pairs accepted per individual API call


@require_GET
def interaction_query_view(request):
    return render(request, "hippie_website/interaction_query.html")


@require_POST
def interaction_query_api(request):
    """
    POST /api/interaction/

    Request body (JSON):
    {
        "pairs": [
            { "a": "<id>", "b": "<id>", "input_order": <int> },
            ...
        ]
    }

    Response (JSON):
    {
        "results": [
            {
                "input_order":     <int>,
                "input_a":         "<raw>",
                "input_b":         "<raw>",
                "symbol_a":        "<gene>",
                "symbol_b":        "<gene>",
                "uniprot_a":       "<id>" | "",
                "uniprot_b":       "<id>" | "",
                "score":           <float>,   # -1.0 if not found
                "source_count":    <int>,     # 0 if not found
                "experiment_count":<int>,     # 0 if not found
                "interaction_id":  <int> | null,
                "detail_url":      "<url>" | ""
            },
            ...
        ]
    }
    """
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON body."}, status=400)

    raw_pairs = body.get("pairs", [])
    if not isinstance(raw_pairs, list):
        return JsonResponse({"error": "'pairs' must be a list."}, status=400)
    if len(raw_pairs) > BATCH_LIMIT:
        return JsonResponse(
            {"error": f"Batch too large: {len(raw_pairs)} pairs (max {BATCH_LIMIT} per request)."},
            status=400,
        )

    results = []
    for item in raw_pairs:
        input_a = str(item.get("a", "")).strip()
        input_b = str(item.get("b", "")).strip()
        input_order = int(item.get("input_order", 0))

        row = _resolve_interaction_pair(input_a, input_b, input_order)
        results.append(row)
    return JsonResponse({"results": results})


def _resolve_interaction_pair(input_a: str, input_b: str, input_order: int) -> dict:
    """
    Resolve two identifiers to proteins, look up their interaction,
    and return a result row.

    A score of -1.0 signals "not found" (either protein unknown, or
    no interaction recorded between them).
    """
    NOT_FOUND = {
        "input_order":      input_order,
        "input_a":          input_a,
        "input_b":          input_b,
        "symbol_a":         input_a,
        "symbol_b":         input_b,
        "uniprot_a":        "",
        "uniprot_b":        "",
        "score":            -1.0,
        "source_count":     0,
        "experiment_count": 0,
        "interaction_id":   None,
        "detail_url":       "",
    }

    protein_a = Protein.objects.resolve(input_a).prefetch_related("uniprot_ids", "entrez_ids").first()
    protein_b = Protein.objects.resolve(input_b).prefetch_related("uniprot_ids", "entrez_ids").first()

    if protein_a is None or protein_b is None:
        return NOT_FOUND

    p1, p2 = (protein_a, protein_b) if protein_a.pk <= protein_b.pk else (protein_b, protein_a)

    try:
        interaction = (
            Interaction.objects
            .with_proteins()
            .prefetch_related("sources", "experiments")
            .get(protein_1=p1, protein_2=p2)
        )
    except Interaction.DoesNotExist:
        # Proteins resolved but no interaction on record
        ua = _protein_display(protein_a)
        ub = _protein_display(protein_b)
        return {
            **NOT_FOUND,
            "symbol_a":  ua["symbol"],
            "symbol_b":  ub["symbol"],
            "uniprot_a": ua["uniprot_id"],
            "uniprot_b": ub["uniprot_id"],
        }

    ua = _protein_display(protein_a)
    ub = _protein_display(protein_b)

    return {
        "input_order": input_order,
        "input_a": input_a,
        "input_b": input_b,
        "symbol_a": ua["symbol"],
        "symbol_b": ub["symbol"],
        "uniprot_a": ua["uniprot_id"],
        "uniprot_b": ub["uniprot_id"],
        "score":            round(interaction.score, 4),
        "source_count":     interaction.sources.all().count(),
        "experiment_count": interaction.experiments.all().count(),
        "interaction_id":   interaction.pk,
        "detail_url":       reverse("hippie_website:interaction_detail", args=[interaction.pk]),
    }





# ---------------------------------------------------------------------------
# Network query
# ---------------------------------------------------------------------------

# Choices passed to the template for the filter form
#_DIRECTIONALITY_CHOICES = [
#    ("any",      "Any"),
#    ("directed", "Directed only"),
#    ("undirected","Undirected only"),
#]

#_EFFECT_CHOICES = [
#    ("activation",  "Activation"),
#    ("inhibition",  "Inhibition"),
#    ("binding",     "Binding"),
#    ("reaction",    "Reaction"),
#    ("other",       "Other / unknown"),
#]


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
    print(result)
    if result.get("error"):
        return JsonResponse(result, status=400)
    return JsonResponse(result)


#def _get_tissue_list():
#    """Return all Tissue names, sorted, for the filter form checkboxes."""
#    return list(Tissue.objects.values_list("name", flat=True).order_by("name"))


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
    protein_ids, unresolved = _protein_ids_from_raw(raw)
    seen = set(protein_ids)

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
    """Render the browse page (React streaming shell)."""
    return render(request, "hippie_website/browse.html", {})


@require_GET
def browse_api(request):
    """
    GET /api/browse/?offset=<int>&limit=<int>
                    &tissue=<id>&source=<id>
                    &min_degree=<int>&min_score=<float>

    Streams the protein list in chunks.  Each response includes:
    {
        "total":    <int>,          # total matching proteins (for progress bar)
        "proteins": [
            {
                "id":        <int>,
                "symbol":    "<gene>",
                "uniprot_id":"<id>" | "",
                "entrez_id": <int> | null,
                "degree":    <int>,
                "avg_score": <float> | null,
            },
            ...
        ]
    }

    Server-side filters applied here: tissue, source, min_degree, min_score.
    Free-text search, sort, and pagination are handled client-side.
    """
    from .models import Tissue, Source
    from django.db.models import Count, Avg, Q as _Q

    # ── Parse params ────────────────────────────────────────────────────
    try:
        offset = max(0, int(request.GET.get("offset", 0)))
        limit  = min(2000, max(1, int(request.GET.get("limit", 500))))
    except (TypeError, ValueError):
        offset, limit = 0, 500

    tissue_id  = request.GET.get("tissue")
    source_id  = request.GET.get("source")
    min_degree = request.GET.get("min_degree")
    min_score  = request.GET.get("min_score")
    print("1")
    # ── Build queryset via manager ──────────────────────────────────────
    qs = Protein.objects.with_browse_annotations()   # annotates degree, avg_score; prefetches uniprot_ids, entrez_ids
    print("2")
    # Filter: tissue expression
    if tissue_id:
        try:
            qs = qs.expressed_in([int(tissue_id)])
        except (TypeError, ValueError):
            pass

    # Filter: source database — proteins that have at least one interaction
    # from this source (on either side)
    if source_id:
        try:
            sid = int(source_id)
            qs = qs.filter(
                _Q(interactions_as_1__sources__id=sid) |
                _Q(interactions_as_2__sources__id=sid)
            ).distinct()
        except (TypeError, ValueError):
            pass

    # Filter: minimum degree  (applied after annotation)
    if min_degree:
        try:
            qs = qs.filter(degree__gte=int(min_degree))
        except (TypeError, ValueError):
            pass

    # Filter: minimum avg score  (applied after annotation)
    if min_score:
        try:
            qs = qs.filter(avg_score__gte=float(min_score))
        except (TypeError, ValueError):
            pass
    print("3")
    # ── Count total (for progress bar) ──────────────────────────────────
    total = 8000#qs.count()
    print("4")
    # ── Fetch slice ──────────────────────────────────────────────────────
    chunk = qs[offset : offset + limit]
    print("5")
    proteins = []
    for p in chunk:
        print("chunk")
        uniprot = p.uniprot_ids.all().first()
        entrez  = p.entrez_ids.all().first()

        # avg_score is the sum of the two side-averages from the annotation;
        # halve it to get an approximate overall average, guard for None.
        raw_avg = getattr(p, "avg_score", None)
        avg = round(raw_avg, 4) if raw_avg is not None else None

        proteins.append({
            "id":        p.pk,
            "symbol":    entrez.name if entrez else p.name,
            "uniprot_id": uniprot.uniprot_id if uniprot else "",
            "entrez_id": entrez.gene_id if entrez else None,
            "degree":    getattr(p, "degree", 0) or 0,
            "avg_score": avg,
        })

    return JsonResponse({"total": total, "proteins": proteins})


@require_GET
def browse_filter_meta(request):
    """
    GET /api/browse/filters/

    Returns the data needed to populate the filter dropdowns:
    {
        "tissues": [{ "id": <int>, "name": "<str>" }, ...],
        "sources": [{ "id": <int>, "name": "<str>" }, ...]
    }
    """
    from .models import Tissue, Source

    tissues = list(
        Tissue.objects.order_by("name").values("id", "name")
    )
    sources = list(
        Source.objects.order_by("name").values("id", "name")
    )
    return JsonResponse({"tissues": tissues, "sources": sources})


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



# ---------------------------------------------------------------------------
# Static pages
# ---------------------------------------------------------------------------

@require_GET
def download_view(request):
    return render(request, "hippie_website/download.html", {})


@require_GET
def information_view(request):
    return render(request, "hippie_website/information.html", {})
