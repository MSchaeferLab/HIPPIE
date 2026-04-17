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
from .models import (
    Interaction,
    Isoform,
    Protein,
    ProteinUniProt,
    UniProtAccession,
    Tissue,
)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _protein_display(protein: Protein, isoform_uid: str | None = None) -> dict:
    """
    Return a compact serialisable dict for a Protein instance.

    Assumes `uniprot_ids` and `entrez_ids` have already been prefetched
    (either by the manager's with_proteins() or an explicit prefetch_related).
    Accessing .all() on a prefetched relation hits the prefetch cache —
    no extra queries.

    isoform_uid: pass the isoform-specific accession (e.g. "P38398-2") explicitly
    when the protein object was fetched as a Protein (not Isoform) queryset.
    """
    uniprot = (
        # prefetch cache is ordered by "-version" when loaded via with_proteins()
        protein.uniprot_ids.all().first()
    )
    entrez = protein.entrez_ids.all().first()
    return {
        "id": protein.pk,
        "name": protein.name,
        "uniprot_id": uniprot.uniprot_id if uniprot else "",
        "gene_id": entrez.gene_id if entrez else None,
        "symbol": entrez.name if entrez else protein.name,
        # isoform_uid is set when this protein is an isoform; None for canonical.
        "isoform_uniprot_id": isoform_uid
        if isoform_uid is not None
        else getattr(protein, "isoform_uniprot_id", None),
    }


def _protein_ids_from_raw(raw: str) -> tuple[list[int], list[str]]:
    """
    Resolve a whitespace-separated string of identifiers to Protein PKs.
    Returns (resolved_pks, unresolved_identifiers).
    """
    protein_ids: list[int] = []
    unresolved: list[str] = []
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


def _get_isoforms(protein_pk: int) -> list:
    """
    Given a canonical protein PK, return all its Isoform objects (with
    uniprot_ids and entrez_ids prefetched for display).

    Returns an empty list when the protein is already an isoform — the
    spec says isoform inputs are never expanded further.

    Resolution path:
        protein_pk → ProteinUniProt (entry names)
                   → UniProtAccession (accessions, e.g. "P38398")
                   → Isoform.isoform_uniprot_id startswith accession + "-"
    """
    # If this protein IS itself an isoform, don't expand.
    if Isoform.objects.filter(protein_ptr_id=protein_pk).exists():
        return []

    uniprot_entry_ids = list(
        ProteinUniProt.objects.filter(protein_id=protein_pk).values_list(
            "uniprot_id", flat=True
        )
    )
    if not uniprot_entry_ids:
        return []

    accessions = list(
        UniProtAccession.objects.filter(uniprot_id__in=uniprot_entry_ids).values_list(
            "accession", flat=True
        )
    )
    if not accessions:
        return []

    iso_q = Q()
    for acc in accessions:
        iso_q |= Q(isoform_uniprot_id__startswith=acc + "-")

    return list(
        Isoform.objects.filter(iso_q).prefetch_related("uniprot_ids", "entrez_ids")
    )


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
    GET /api/query/?q=<identifier>[&include_isoforms=1]

    Resolves the identifier via Protein.objects.resolve(), then fetches
    all interactions via Interaction.objects.for_proteins().with_proteins()
    so that partner identifier mappings are available in a single round-trip.

    When include_isoforms=1 and the resolved protein is a canonical (not
    itself an isoform), the query is also run for every known isoform of
    that protein, and all results are returned together.

    Response shape:
    {
        "query_protein":     { id, name, uniprot_id, gene_id, symbol, isoform_uniprot_id },
        "isoforms_included": <bool>,
        "expanded_proteins": [ ...same shape... ],   // isoforms added to the query
        "interactions": [
            {
                "id":               <int>,
                "query_side":       { ...protein dict... },  // which protein was on query side
                "partner":          { ...protein dict... },
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
    include_isoforms = request.GET.get("include_isoforms", "") in ("1", "true", "yes")

    if not q:
        return JsonResponse(
            {
                "error": "No query provided.",
                "interactions": [],
                "query_protein": None,
            }
        )

    # ── Resolve identifier → Protein ──────────────────────────────────
    proteins = Protein.objects.resolve(q).prefetch_related("uniprot_ids", "entrez_ids")

    if not proteins.exists():
        return JsonResponse(
            {
                "error": f"No protein found for '{q}'.",
                "interactions": [],
                "query_protein": None,
            }
        )

    protein = proteins.first()

    # ── Optionally expand to isoforms ─────────────────────────────────
    isoforms: list = []
    protein_pks: list[int] = [protein.pk]
    if include_isoforms:
        isoforms = _get_isoforms(protein.pk)
        protein_pks.extend(iso.pk for iso in isoforms)

    # Map PK → isoform_uniprot_id for every expanded isoform (used in display).
    isoform_uid_map: dict[int, str] = {
        iso.pk: iso.isoform_uniprot_id for iso in isoforms
    }
    protein_pks_set = set(protein_pks)

    # ── Fetch interactions ─────────────────────────────────────────────
    # for_proteins() handles a single-element list the same as for_protein().
    interactions_qs = (
        Interaction.objects.for_proteins(protein_pks)
        .with_proteins()
        .prefetch_related("sources", "experiments")
        .order_by("-score")
    )

    results = []
    for interaction in interactions_qs:
        # Determine which side is on the query side and which is the partner.
        if interaction.protein_1_id in protein_pks_set:
            query_side, partner = interaction.protein_1, interaction.protein_2
        else:
            query_side, partner = interaction.protein_2, interaction.protein_1

        results.append(
            {
                "id": interaction.pk,
                "query_side": _protein_display(
                    query_side, isoform_uid_map.get(query_side.pk)
                ),
                "partner": _protein_display(partner),
                "score": round(interaction.score, 4),
                "source_count": interaction.sources.all().count(),
                "experiment_count": interaction.experiments.all().count(),
                "detail_url": reverse(
                    "hippie_website:interaction_detail", args=[interaction.pk]
                ),
            }
        )

    return JsonResponse(
        {
            "query_protein": _protein_display(protein),
            "isoforms_included": include_isoforms,
            "expanded_proteins": [_protein_display(iso) for iso in isoforms],
            "interactions": results,
            "error": None,
        }
    )


# ---------------------------------------------------------------------------
# Interaction query
# ---------------------------------------------------------------------------

MAX_PAIRS = 5_000  # hard limit enforced server-side and client-site
BATCH_LIMIT = 200  # max pairs accepted per individual API call


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
        ],
        "include_isoforms": <bool>   // optional, default false
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
    include_isoforms = bool(body.get("include_isoforms", False))

    if not isinstance(raw_pairs, list):
        return JsonResponse({"error": "'pairs' must be a list."}, status=400)
    if len(raw_pairs) > BATCH_LIMIT:
        return JsonResponse(
            {
                "error": f"Batch too large: {len(raw_pairs)} pairs (max {BATCH_LIMIT} per request)."
            },
            status=400,
        )

    # Per-request cache so repeated proteins in a batch share isoform lookups.
    isoform_cache: dict[int, list] = {}

    results = []
    for item in raw_pairs:
        input_a = str(item.get("a", "")).strip()
        input_b = str(item.get("b", "")).strip()
        input_order = int(item.get("input_order", 0))

        if include_isoforms:
            rows = _resolve_interaction_pair_with_isoforms(
                input_a, input_b, input_order, isoform_cache
            )
        else:
            rows = [_resolve_interaction_pair(input_a, input_b, input_order)]
        results.extend(rows)
    return JsonResponse({"results": results})


def _resolve_interaction_pair(input_a: str, input_b: str, input_order: int) -> dict:
    """
    Resolve two identifiers to proteins, look up their interaction,
    and return a result row.

    A score of -1.0 signals "not found" (either protein unknown, or
    no interaction recorded between them).
    """
    NOT_FOUND = {
        "input_order": input_order,
        "input_a": input_a,
        "input_b": input_b,
        "symbol_a": input_a,
        "symbol_b": input_b,
        "uniprot_a": "",
        "uniprot_b": "",
        "score": -1.0,
        "source_count": 0,
        "experiment_count": 0,
        "interaction_id": None,
        "detail_url": "",
    }

    protein_a = (
        Protein.objects.resolve(input_a)
        .prefetch_related("uniprot_ids", "entrez_ids")
        .first()
    )
    protein_b = (
        Protein.objects.resolve(input_b)
        .prefetch_related("uniprot_ids", "entrez_ids")
        .first()
    )

    if protein_a is None or protein_b is None:
        return NOT_FOUND

    p1, p2 = (
        (protein_a, protein_b)
        if protein_a.pk <= protein_b.pk
        else (protein_b, protein_a)
    )

    try:
        interaction = (
            Interaction.objects.with_proteins()
            .prefetch_related("sources", "experiments")
            .get(protein_1=p1, protein_2=p2)
        )
    except Interaction.DoesNotExist:
        # Proteins resolved but no interaction on record
        ua = _protein_display(protein_a)
        ub = _protein_display(protein_b)
        return {
            **NOT_FOUND,
            "symbol_a": ua["symbol"],
            "symbol_b": ub["symbol"],
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
        "isoform_uniprot_a": ua["isoform_uniprot_id"],
        "isoform_uniprot_b": ub["isoform_uniprot_id"],
        "score": round(interaction.score, 4),
        "source_count": interaction.sources.all().count(),
        "experiment_count": interaction.experiments.all().count(),
        "interaction_id": interaction.pk,
        "detail_url": reverse(
            "hippie_website:interaction_detail", args=[interaction.pk]
        ),
    }


def _resolve_interaction_pair_with_isoforms(
    input_a: str,
    input_b: str,
    input_order: int,
    isoform_cache: dict,
) -> list[dict]:
    """
    Like _resolve_interaction_pair but expands each canonical protein side to
    include all its known isoforms, then checks every resulting combination for
    a recorded interaction.

    Rules (matching the spec):
      • If a resolved protein IS an isoform, that side is NOT expanded further.
      • If a resolved protein is canonical, expand to canonical + all isoforms.
      • Only interactions that actually exist in the database are returned.
      • If no combination has a recorded interaction, fall back to returning the
        original pair as "not found" (score = -1), preserving the existing UX.

    isoform_cache: a per-request dict[protein_pk -> list[Isoform]] to avoid
    repeated DB lookups when the same protein appears in multiple pairs.
    """
    protein_a = (
        Protein.objects.resolve(input_a)
        .prefetch_related("uniprot_ids", "entrez_ids")
        .first()
    )
    protein_b = (
        Protein.objects.resolve(input_b)
        .prefetch_related("uniprot_ids", "entrez_ids")
        .first()
    )

    if protein_a is None or protein_b is None:
        return [_resolve_interaction_pair(input_a, input_b, input_order)]

    # Cached isoform lookup ---------------------------------------------------
    def cached_isoforms(pk: int) -> list:
        if pk not in isoform_cache:
            isoform_cache[pk] = _get_isoforms(pk)
        return isoform_cache[pk]

    isoforms_a = cached_isoforms(protein_a.pk)
    isoforms_b = cached_isoforms(protein_b.pk)

    a_pks: list[int] = [protein_a.pk] + [iso.pk for iso in isoforms_a]
    b_pks: list[int] = [protein_b.pk] + [iso.pk for iso in isoforms_b]

    # Load all relevant proteins in one query for display --------------------
    all_pks = list(set(a_pks + b_pks))
    proteins_map: dict[int, Protein] = {
        p.pk: p
        for p in Protein.objects.filter(pk__in=all_pks).prefetch_related(
            "uniprot_ids", "entrez_ids"
        )
    }

    # Build isoform UID map (pk → isoform-specific accession) ----------------
    isoform_uid_map: dict[int, str] = {
        iso.protein_ptr_id: iso.isoform_uniprot_id
        for iso in Isoform.objects.filter(protein_ptr_id__in=all_pks)
    }

    # Build the set of canonical (p1_pk, p2_pk) pairs with their a/b origin --
    # (p1_pk <= p2_pk as required by the Interaction model constraint)
    canonical_pairs: dict[tuple[int, int], tuple[int, int]] = {}
    for pa_pk in a_pks:
        for pb_pk in b_pks:
            if pa_pk == pb_pk:
                continue
            p1_pk, p2_pk = (min(pa_pk, pb_pk), max(pa_pk, pb_pk))
            if (p1_pk, p2_pk) not in canonical_pairs:
                # Store which pk was on the A side and which was on the B side
                # (for correct display ordering in the response).
                canonical_pairs[(p1_pk, p2_pk)] = (pa_pk, pb_pk)

    if not canonical_pairs:
        return [_resolve_interaction_pair(input_a, input_b, input_order)]

    # Fetch all interactions in a single query --------------------------------
    q = Q()
    for p1_pk, p2_pk in canonical_pairs:
        q |= Q(protein_1_id=p1_pk, protein_2_id=p2_pk)

    found_interactions: dict[tuple[int, int], Interaction] = {
        (i.protein_1_id, i.protein_2_id): i
        for i in (
            Interaction.objects.with_proteins()
            .prefetch_related("sources", "experiments")
            .filter(q)
        )
    }

    # Build result rows -------------------------------------------------------
    found_results: list[dict] = []
    for (p1_pk, p2_pk), (pa_pk, pb_pk) in canonical_pairs.items():
        interaction = found_interactions.get((p1_pk, p2_pk))
        if not interaction:
            continue

        pa = proteins_map.get(pa_pk)
        pb = proteins_map.get(pb_pk)
        if pa is None or pb is None:
            continue

        ua = _protein_display(pa, isoform_uid_map.get(pa_pk))
        ub = _protein_display(pb, isoform_uid_map.get(pb_pk))
        found_results.append(
            {
                "input_order": input_order,
                "input_a": input_a,
                "input_b": input_b,
                "symbol_a": ua["symbol"],
                "symbol_b": ub["symbol"],
                "uniprot_a": ua["uniprot_id"],
                "uniprot_b": ub["uniprot_id"],
                "isoform_uniprot_a": ua["isoform_uniprot_id"],
                "isoform_uniprot_b": ub["isoform_uniprot_id"],
                "score": round(interaction.score, 4),
                "source_count": interaction.sources.all().count(),
                "experiment_count": interaction.experiments.all().count(),
                "interaction_id": interaction.pk,
                "detail_url": reverse(
                    "hippie_website:interaction_detail", args=[interaction.pk]
                ),
            }
        )

    # If no isoform combination found anything, show original pair as not-found.
    if not found_results:
        return [_resolve_interaction_pair(input_a, input_b, input_order)]

    return found_results


# ---------------------------------------------------------------------------
# Network query
# ---------------------------------------------------------------------------

# Choices passed to the template for the filter form
# _DIRECTIONALITY_CHOICES = [
#    ("any",      "Any"),
#    ("directed", "Directed only"),
#    ("undirected","Undirected only"),
# ]

# _EFFECT_CHOICES = [
#    ("activation",  "Activation"),
#    ("inhibition",  "Inhibition"),
#    ("binding",     "Binding"),
#    ("reaction",    "Reaction"),
#    ("other",       "Other / unknown"),
# ]


class NetworkQueryView(FormView):
    """
    GET  → render the blank NetworkQueryForm.
    POST → validate, run the query, re-render with results.

    _run_network_query() is also called by the JSON API endpoint so the
    query logic lives in one place.
    """

    template_name = "hippie_website/network_query.html"
    form_class = NetworkQueryForm

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
            "proteins": raw_proteins,
            "expand": expand,
            "score_min": cd.get("score_min") or 0.0,
            "directionality": {
                "none": "any",
                "kegg": "directed",
                "unweighted_sp": "any",
                "weighted_sp": "any",
            }.get(cd.get("direction", "none"), "any"),
            "effect_type": {
                "predicted": ["activation", "inhibition"],
                "kegg": ["activation", "inhibition"],
            }.get(cd.get("effect", "none"), []),
            "tissues": [cd["tissue"].name] if cd.get("tissue") else [],
            "go_terms": cd.get("go_terms", ""),
            "mesh_terms": cd.get("mesh_terms", ""),
        }
        result = _run_network_query(params)
        output_type = cd.get("output_type", "browser_vis")
        return self.render_to_response(
            self.get_context_data(
                form=form,
                network_result=result,
                output_type=output_type,
                cy_edges_json=json.dumps(result["interactions"])
                if output_type == "browser_vis"
                else "[]",
                cy_seeds_json=json.dumps(result.get("seed_proteins", []))
                if output_type == "browser_vis"
                else "[]",
            )
        )


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


# def _get_tissue_list():
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
            "node_count": 0,
            "edge_count": 0,
            "interactions": [],
            "error": f"None of the identifiers could be resolved: {', '.join(unresolved)}",
        }

    # -- 2. Expansion (layer) ---------------------------------------------
    expand = params.get("expand", "none")
    layer = 0 if expand == "none" else 1  # second_shell treated as layer 1 for now

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
        p1_entrez = ix.protein_1.entrez_ids.all().first()
        p2_uniprot = ix.protein_2.uniprot_ids.all().first()
        p2_entrez = ix.protein_2.entrez_ids.all().first()
        interactions.append(
            {
                "interaction_id": ix.pk,
                "protein_a": ix.protein_1.name,
                "uniprot_a": p1_uniprot.uniprot_id if p1_uniprot else "",
                "entrez_a": p1_entrez.gene_id if p1_entrez else "",
                "gene_name_a": p1_entrez.name if p1_entrez else ix.protein_1.name,
                "protein_b": ix.protein_2.name,
                "uniprot_b": p2_uniprot.uniprot_id if p2_uniprot else "",
                "entrez_b": p2_entrez.gene_id if p2_entrez else "",
                "gene_name_b": p2_entrez.name if p2_entrez else ix.protein_2.name,
                "score": round(ix.score, 4),
                "source_count": ix.sources.all().count(),
                "experiment_count": ix.experiments.all().count(),
                "uploaded_interaction": ix.protein_1_id in seen
                and ix.protein_2_id in seen,
                "kegg_direction": ix.kegg_direction,
                "effect_type": ix.effect_type,
            }
        )

    seed_names = set(
        Protein.objects.filter(pk__in=protein_ids).values_list("name", flat=True)
    )

    return {
        "node_count": len(node_ids),
        "edge_count": len(interactions),
        "interactions": interactions,
        "seed_proteins": list(seed_names),
        "unresolved": unresolved,
        "error": None,
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

    Performance notes
    -----------------
    The client streams all proteins in ~500-row chunks.  The old
    implementation annotated every chunk with ``degree`` (two joins + GROUP
    BY) and ``avg_score`` (a correlated RawSQL subquery that ran once per
    protein row) and then called ``qs.count()`` on the annotated queryset,
    which forces Django to wrap the whole annotated query in a subquery and
    count it — catastrophically slow for tens of thousands of proteins
    across millions of interactions.

    Here we avoid those annotations entirely.  Degree and avg_score are
    always computed via three GROUP BYs on ``interaction`` (one per FK side,
    plus a self-loop correction) which go through the existing
    ``(protein_1, score)`` / ``(protein_2, score)`` covering indexes as
    index-only scans.

      * No ``min_degree`` / ``min_score``: count + slice on the lean
        queryset, then compute stats only for the ~500 sliced IDs.
      * ``min_degree`` / ``min_score`` set: compute stats once across every
        candidate protein, then filter + sort + slice in Python.  The
        aggregates stay fast because they still only touch
        ``interaction`` and its indexes — the DB never has to JOIN
        ``protein`` against ``interaction`` twice with a GROUP BY.
    """
    from .models import ProteinUniProt
    from django.db.models import Prefetch, Q as _Q

    # ── Parse params ────────────────────────────────────────────────────
    try:
        offset = max(0, int(request.GET.get("offset", 0)))
        limit = min(2000, max(1, int(request.GET.get("limit", 500))))
    except (TypeError, ValueError):
        offset, limit = 0, 500

    tissue_id = request.GET.get("tissue")
    source_id = request.GET.get("source")
    min_degree = request.GET.get("min_degree")
    min_score = request.GET.get("min_score")

    # ── Build a lean queryset (no annotations) ─────────────────────────
    base_qs = Protein.objects.all()
    has_scope_filter = False

    if tissue_id:
        try:
            base_qs = base_qs.expressed_in([int(tissue_id)])
            has_scope_filter = True
        except (TypeError, ValueError):
            pass

    # Proteins that have at least one interaction from this source
    # (on either side).
    if source_id:
        try:
            sid = int(source_id)
            base_qs = base_qs.filter(
                _Q(interactions_as_1__sources__id=sid)
                | _Q(interactions_as_2__sources__id=sid)
            ).distinct()
            has_scope_filter = True
        except (TypeError, ValueError):
            pass

    # ``min_degree=0`` / ``min_score=0`` are no-ops, treat as absent.
    min_degree_val = _safe_int(min_degree)
    min_score_val = _safe_float(min_score)
    if min_degree_val is not None and min_degree_val <= 0:
        min_degree_val = None
    if min_score_val is not None and min_score_val <= 0:
        min_score_val = None
    needs_degree_filter = min_degree_val is not None or min_score_val is not None

    base_qs = base_qs.order_by("pk")

    # ── Decide the slice and precompute stats ──────────────────────────
    if needs_degree_filter:
        # We need degree / avg_score to know which proteins pass the filter,
        # so compute them once across the scoped candidates and filter in
        # Python.  When no tissue/source filter narrows the scope, pass
        # ``None`` so the aggregates run against all interactions (no IN
        # list) — that's the shape the query planner likes best.
        scope = None if not has_scope_filter else base_qs.values("pk")
        side1, side2, self_loops = _protein_stats(scope)

        base_pids = list(base_qs.values_list("pk", flat=True))
        matching = []
        for pid in base_pids:
            c1, s1 = side1.get(pid, (0, 0.0))
            c2, s2 = side2.get(pid, (0, 0.0))
            cl, sl = self_loops.get(pid, (0, 0.0))

            degree = c1 + c2
            unique = c1 + c2 - cl
            avg = (s1 + s2 - sl) / unique if unique > 0 else None

            if min_degree_val is not None and degree < min_degree_val:
                continue
            if min_score_val is not None and (avg is None or avg < min_score_val):
                continue
            matching.append(pid)

        total = len(matching)
        pid_slice = matching[offset : offset + limit]
    else:
        # Fast path: no degree/score filter — cheap count, slice IDs, then
        # compute stats for only the sliced IDs.
        total = base_qs.count()
        pid_slice = list(base_qs.values_list("pk", flat=True)[offset : offset + limit])
        if pid_slice:
            side1, side2, self_loops = _protein_stats(pid_slice)
        else:
            side1, side2, self_loops = {}, {}, {}

    if not pid_slice:
        return JsonResponse({"total": total, "proteins": []})

    # ── Fetch proteins w/ prefetched identifier mappings ────────────────
    proteins_by_pk = {
        p.pk: p
        for p in Protein.objects.filter(pk__in=pid_slice).prefetch_related(
            Prefetch(
                "uniprot_ids",
                queryset=ProteinUniProt.objects.order_by("-version"),
            ),
            Prefetch("entrez_ids"),
        )
    }

    # ── Build response in slice order ───────────────────────────────────
    proteins = []
    for pid in pid_slice:
        p = proteins_by_pk.get(pid)
        if p is None:
            continue  # defensive; pk__in should cover every slice id

        c1, s1 = side1.get(pid, (0, 0.0))
        c2, s2 = side2.get(pid, (0, 0.0))
        cl, sl = self_loops.get(pid, (0, 0.0))

        degree = c1 + c2
        unique_count = c1 + c2 - cl
        avg = round((s1 + s2 - sl) / unique_count, 4) if unique_count > 0 else None

        uniprot_list = list(p.uniprot_ids.all())
        entrez_list = list(p.entrez_ids.all())
        uniprot = uniprot_list[0] if uniprot_list else None
        entrez = entrez_list[0] if entrez_list else None

        proteins.append(
            {
                "id": pid,
                "symbol": entrez.name if entrez else p.name,
                "uniprot_id": uniprot.uniprot_id if uniprot else "",
                "entrez_id": entrez.gene_id if entrez else None,
                "degree": degree,
                "avg_score": avg,
            }
        )

    return JsonResponse({"total": total, "proteins": proteins})


def _protein_stats(scope):
    """
    Compute interaction-derived stats per protein.

    Returns three dicts keyed by protein id::

        side1      : pid -> (count(i : i.p1 = pid), sum(score))
        side2      : pid -> (count(i : i.p2 = pid), sum(score))
        self_loops : pid -> (count(i : i.p1 = i.p2 = pid), sum(score))

    ``scope``:
      * ``None``            — aggregate over every interaction row.  Use
                              this when no upstream filter narrows the set
                              of proteins; it's the shape the query planner
                              likes best.
      * iterable of PKs     — restrict aggregates to interactions whose
                              relevant FK side is in this collection.  A
                              list (for small slices) or a subquery
                              ``QuerySet`` (``base_qs.values("pk")``, for
                              large candidate sets) both work.

    Each aggregate is a ``GROUP BY protein_[1|2]_id`` on ``interaction`` —
    the ``(protein_1, score)`` / ``(protein_2, score)`` indexes are covering,
    so these run as index-only scans.
    """
    from django.db.models import Count, F, Sum

    if scope is None:
        side1_qs = Interaction.objects.all()
        side2_qs = Interaction.objects.all()
        self_qs = Interaction.objects.filter(protein_1_id=F("protein_2_id"))
    else:
        if isinstance(scope, (list, tuple, set)) and not scope:
            return {}, {}, {}
        side1_qs = Interaction.objects.filter(protein_1_id__in=scope)
        side2_qs = Interaction.objects.filter(protein_2_id__in=scope)
        self_qs = Interaction.objects.filter(
            protein_1_id__in=scope,
            protein_1_id=F("protein_2_id"),
        )

    def _group(qs, col):
        return {
            row[col]: (row["cnt"], row["sm"] or 0.0)
            for row in qs.values(col).annotate(cnt=Count("id"), sm=Sum("score"))
        }

    return (
        _group(side1_qs, "protein_1_id"),
        _group(side2_qs, "protein_2_id"),
        _group(self_qs, "protein_1_id"),
    )


def _safe_int(value):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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

    tissues = list(Tissue.objects.order_by("name").values("id", "name"))
    sources = list(Source.objects.order_by("name").values("id", "name"))
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
    p1_entrez = interaction.protein_1.entrez_ids.all().first()
    p2_entrez = interaction.protein_2.entrez_ids.all().first()

    # Compute bait-prey detection stats from prefetched data (no extra queries).
    all_tests = [
        test
        for assoc in interaction.bait_prey.all()
        for test in assoc.tests_performed.all()
    ]
    bait_prey_total_tested = len(all_tests)
    bait_prey_times_observed = sum(1 for t in all_tests if t.detection)

    context = {
        "interaction": interaction,
        "p1": {
            "protein": interaction.protein_1,
            "uniprot_id": p1_uniprot.uniprot_id if p1_uniprot else "",
            "gene_id": p1_entrez.gene_id if p1_entrez else None,
            "symbol": p1_entrez.name if p1_entrez else interaction.protein_1.name,
        },
        "p2": {
            "protein": interaction.protein_2,
            "uniprot_id": p2_uniprot.uniprot_id if p2_uniprot else "",
            "gene_id": p2_entrez.gene_id if p2_entrez else None,
            "symbol": p2_entrez.name if p2_entrez else interaction.protein_2.name,
        },
        # All prefetched — .all() hits the cache.
        "sources": interaction.sources.all(),
        "publications": interaction.publications.all(),
        "experiments": interaction.experiments.all().order_by("-quality_score"),
        "species": interaction.conserved_species.all(),
        # Bait-prey detection stats.
        "bait_prey_total_tested": bait_prey_total_tested,
        "bait_prey_times_observed": bait_prey_times_observed,
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
    entrez = protein.entrez_ids.all().first()

    # Count interactions using the manager for consistency.
    interaction_count = Interaction.objects.for_protein(protein.pk).count()

    context = {
        "protein": protein,
        "uniprot_id": uniprot.uniprot_id if uniprot else "",
        "gene_id": entrez.gene_id if entrez else None,
        "symbol": entrez.name if entrez else protein.name,
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
