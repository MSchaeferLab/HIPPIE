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

import hashlib
import json
from django.core.cache import cache
from django.db.models import Exists, OuterRef, Q, Subquery
from django.http import FileResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST
from django.views.generic.edit import FormView

from .forms import NetworkQueryForm
from .models import (
    Interaction,
    Isoform,
    OrthologInteraction,
    Protein,
    Tissue,
    NonInteraction,
    SplitJob,
    InteractionType,
)

from .tasks import run_split_job


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _protein_display(protein: Protein, isoform_uid: str | None = None) -> dict:
    """
    Return a compact serialisable dict for a Protein instance.

    Assumes `gene` has already been select_related (either by the manager's
    with_proteins() or an explicit select_related("gene")).

    isoform_uid: pass the isoform-specific accession (e.g. "P38398-2") explicitly
    when the protein object was fetched as a Protein (not Isoform) queryset.
    """
    gene = protein.gene
    return {
        "id": protein.pk,
        "name": gene.entrez_name or protein.uniprot_name,
        "uniprot_id": protein.uniprot_accession,
        "gene_id": gene.entrez_id or None,
        "symbol": gene.entrez_name or protein.uniprot_name,
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
    Given a canonical protein PK, return all its Isoform objects.

    Returns an empty list when the protein is already an isoform — the
    spec says isoform inputs are never expanded further.

    Resolution path:
        protein_pk → Protein.uniprot_accession (e.g. "P38398")
                   → Isoform.uniprot_accession startswith accession + "-"
    """
    # If this protein IS itself an isoform, don't expand.
    if Isoform.objects.filter(protein_ptr_id=protein_pk).exists():
        return []

    try:
        accession = Protein.objects.values_list("uniprot_accession", flat=True).get(
            pk=protein_pk
        )
    except Protein.DoesNotExist:
        return []

    if not accession:
        return []

    return list(
        Isoform.objects.filter(
            uniprot_accession__startswith=accession + "-"
        ).select_related("gene")
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
    show = request.GET.get("show", "interactions")
    if show not in ("interactions", "noninteractions", "both"):
        show = "interactions"

    if not q:
        return JsonResponse(
            {
                "error": "No query provided.",
                "interactions": [],
                "query_protein": None,
            }
        )

    # ── Resolve identifier → Protein ──────────────────────────────────
    proteins = Protein.objects.resolve(q).select_related("gene")

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

    # Map PK → isoform accession for every expanded isoform (used in display).
    isoform_uid_map: dict[int, str] = {
        iso.pk: iso.uniprot_accession for iso in isoforms
    }
    protein_pks_set = set(protein_pks)

    # ── Fetch interactions and/or non-interactions -──────────────────
    # for_proteins() handles a single-element list the same as for_protein().

    results = []
    if show in ("interactions", "both"):
        interactions_qs = (
            Interaction.objects.for_proteins(protein_pks)
            .with_proteins()
            .prefetch_related("sources", "experiments")
            .order_by("-score")
        )
        if not include_isoforms:
            # Drop interactions whose partner is an isoform, but keep any
            # isoform the user explicitly queried (protein_pks). A side is
            # allowed when it is canonical OR is a queried protein. FK anti-join
            # instead of a ``NOT IN (7.6k-row subquery)`` ORed across both sides.
            interactions_qs = interactions_qs.filter(
                (Q(protein_1__isoform__isnull=True) | Q(protein_1_id__in=protein_pks))
                & (Q(protein_2__isoform__isnull=True) | Q(protein_2_id__in=protein_pks))
            )
        for interaction in interactions_qs:
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
                    "is_noninteraction": False,
                    "detail_url": reverse(
                        "hippie_website:interaction_detail", args=[interaction.pk]
                    ),
                }
            )

    if show in ("noninteractions", "both"):
        noninteractions_qs = (
            NonInteraction.objects.filter(
                Q(protein_1_id__in=protein_pks) | Q(protein_2_id__in=protein_pks)
            )
            .select_related(
                "protein_1", "protein_1__gene", "protein_2", "protein_2__gene"
            )
            .order_by("-score")
        )
        if not include_isoforms:
            # See the interactions branch above — same canonical-or-queried rule.
            noninteractions_qs = noninteractions_qs.filter(
                (Q(protein_1__isoform__isnull=True) | Q(protein_1_id__in=protein_pks))
                & (Q(protein_2__isoform__isnull=True) | Q(protein_2_id__in=protein_pks))
            )
        for ni in noninteractions_qs:
            if ni.protein_1_id in protein_pks_set:
                query_side, partner = ni.protein_1, ni.protein_2
            else:
                query_side, partner = ni.protein_2, ni.protein_1
            results.append(
                {
                    "id": ni.pk,
                    "query_side": _protein_display(
                        query_side, isoform_uid_map.get(query_side.pk)
                    ),
                    "partner": _protein_display(partner),
                    "score": round(ni.score, 4),
                    "source_count": None,
                    "experiment_count": None,
                    "is_noninteraction": True,
                    "detail_url": reverse(
                        "hippie_website:noninteraction_detail", args=[ni.pk]
                    ),
                }
            )

    # For "both" mode, re-sort by score descending (interactions first for ties)
    if show == "both":
        results.sort(key=lambda r: r["score"], reverse=True)

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
    show = body.get("show", "interactions")
    if show not in ("interactions", "noninteractions", "both"):
        show = "interactions"

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
            # Isoform expansion only applies to the Interaction table.
            int_rows: list[dict] = []
            if show in ("interactions", "both"):
                int_rows = _resolve_interaction_pair_with_isoforms(
                    input_a, input_b, input_order, isoform_cache
                )
            nonint_rows: list[dict] = []
            if show in ("noninteractions", "both"):
                nr = _resolve_noninteraction_pair(input_a, input_b, input_order)
                if nr["score"] >= 0:
                    nonint_rows = [nr]
            rows = int_rows + nonint_rows
            if not rows:
                # Nothing found in either table — return a single not-found row.
                if show == "noninteractions":
                    rows = [_resolve_noninteraction_pair(input_a, input_b, input_order)]
                else:
                    rows = [_resolve_interaction_pair(input_a, input_b, input_order)]
        else:
            if show == "interactions":
                rows = [_resolve_interaction_pair(input_a, input_b, input_order)]
            elif show == "noninteractions":
                rows = [_resolve_noninteraction_pair(input_a, input_b, input_order)]
            else:  # both
                int_row = _resolve_interaction_pair(input_a, input_b, input_order)
                nonint_row = _resolve_noninteraction_pair(input_a, input_b, input_order)
                found = [r for r in [int_row, nonint_row] if r["score"] >= 0]
                rows = found if found else [int_row]

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
        "isoform_uniprot_a": None,
        "isoform_uniprot_b": None,
        "score": -1.0,
        "source_count": 0,
        "experiment_count": 0,
        "interaction_id": None,
        "detail_url": "",
    }

    protein_a = Protein.objects.resolve(input_a).select_related("gene").first()
    protein_b = Protein.objects.resolve(input_b).select_related("gene").first()

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


def _resolve_noninteraction_pair(input_a: str, input_b: str, input_order: int) -> dict:
    """
    Resolve two identifiers to proteins, look up their non-interaction record
    in the NonInteraction table, and return a result row.

    A score of -1.0 signals "not found".  Non-interactions never have source or
    experiment counts (those fields are None in the response).
    """
    NOT_FOUND = {
        "input_order": input_order,
        "input_a": input_a,
        "input_b": input_b,
        "symbol_a": input_a,
        "symbol_b": input_b,
        "uniprot_a": "",
        "uniprot_b": "",
        "isoform_uniprot_a": None,
        "isoform_uniprot_b": None,
        "score": -1.0,
        "source_count": None,
        "experiment_count": None,
        "is_noninteraction": True,
        "interaction_id": None,
        "detail_url": "",
    }

    protein_a = Protein.objects.resolve(input_a).select_related("gene").first()
    protein_b = Protein.objects.resolve(input_b).select_related("gene").first()

    if protein_a is None or protein_b is None:
        return NOT_FOUND

    p1, p2 = (
        (protein_a, protein_b)
        if protein_a.pk <= protein_b.pk
        else (protein_b, protein_a)
    )

    try:
        ni = NonInteraction.objects.get(protein_1=p1, protein_2=p2)
    except NonInteraction.DoesNotExist:
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
        "score": round(ni.score, 4),
        "source_count": None,
        "experiment_count": None,
        "is_noninteraction": True,
        "interaction_id": ni.pk,
        "detail_url": reverse("hippie_website:noninteraction_detail", args=[ni.pk]),
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
    protein_a = Protein.objects.resolve(input_a).select_related("gene").first()
    protein_b = Protein.objects.resolve(input_b).select_related("gene").first()

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
        p.pk: p for p in Protein.objects.filter(pk__in=all_pks).select_related("gene")
    }

    # Build isoform UID map (pk → isoform-specific accession) ----------------
    isoform_uid_map: dict[int, str] = {
        iso.protein_ptr_id: iso.uniprot_accession
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

        selected_types = cd.get("interaction_types")
        params = {
            "proteins": raw_proteins,
            "expand": expand,
            "include_isoforms": cd.get("include_isoforms", False),
            "score_min": cd.get("score_min") or 0.0,
            "tissues": [cd["tissue"].name] if cd.get("tissue") else [],
            "min_rpkm": cd.get("min_rpkm", None),
            "interaction_type_ids": [t.pk for t in selected_types] if selected_types else [],
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
        tissue_mode     — "any" | "both" | "one"  (unused: we always require both)
        tissues         — list of tissue names
    """
    # -- 1. Resolve seed proteins -----------------------------------------
    raw = params.get("proteins", "")
    protein_ids, unresolved = _protein_ids_from_raw(raw)
    seen = set(protein_ids)

    if params.get("include_isoforms", False):
        isoform_pks: list[int] = []
        for pk in list(protein_ids):
            isoform_pks.extend(iso.pk for iso in _get_isoforms(pk))
        protein_ids = list(dict.fromkeys(protein_ids + isoform_pks))

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

    min_rpkm = params.get("min_rpkm")
    try:
        min_rpkm_val: float | None = float(min_rpkm) if min_rpkm else None
    except (TypeError, ValueError):
        min_rpkm_val = None

    # -- 5. Core queryset -------------------------------------------------
    qs = Interaction.objects.network_query(
        protein_ids,
        layer=layer,
        score_threshold=score_min,
        tissue_ids=tissue_ids,
        min_rpkm=min_rpkm_val,
    ).prefetch_related("sources", "experiments")

    # -- 5a. Interaction type filter --------------------------------------
    if hasattr(params, "getlist"):
        type_ids = [int(i) for i in params.getlist("interaction_type_ids") if i]
    else:
        type_ids = params.get("interaction_type_ids") or []
    if type_ids:
        qs = qs.of_types(type_ids)

    if not params.get("include_isoforms", False):
        isoform_pks = Isoform.objects.values_list("protein_ptr_id", flat=True)
        qs = qs.exclude(protein_1_id__in=isoform_pks).exclude(
            protein_2_id__in=isoform_pks
        )

    # -- 6. Serialise -----------------------------------------------------
    interactions = []
    node_ids: set[int] = set()
    for ix in qs:
        node_ids.add(ix.protein_1_id)
        node_ids.add(ix.protein_2_id)
        p1 = ix.protein_1
        p2 = ix.protein_2
        interactions.append(
            {
                "interaction_id": ix.pk,
                "protein_a": p1.gene.entrez_name or p1.uniprot_name,
                "uniprot_a": p1.uniprot_accession,
                "entrez_a": p1.gene.entrez_id or "",
                "gene_name_a": p1.gene.entrez_name or p1.uniprot_name,
                "protein_b": p2.gene.entrez_name or p2.uniprot_name,
                "uniprot_b": p2.uniprot_accession,
                "entrez_b": p2.gene.entrez_id or "",
                "gene_name_b": p2.gene.entrez_name or p2.uniprot_name,
                "score": round(ix.score, 4),
                "source_count": len(ix.sources.all()),
                "experiment_count": len(ix.experiments.all()),
                "uploaded_interaction": ix.protein_1_id in seen
                and ix.protein_2_id in seen,
            }
        )

    seed_names = set(
        Protein.objects.filter(pk__in=protein_ids).values_list(
            "gene__entrez_name", flat=True
        )
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


_BROWSE_SORT_FIELDS = {
    "symbol": "gene__entrez_name",
    "uniprot_id": "uniprot_accession",
    "entrez_id": "gene__entrez_id",
    "degree": "degree",
    "avg_score": "avg_score",
}


def _protein_search_q(q: str) -> Q:
    """
    Build the free-text search ``Q`` used by the browse pages: case-insensitive
    match on gene symbol, UniProt accession, or UniProt entry name; exact match
    on the Entrez gene ID when the query is all digits.
    """
    cond = (
        Q(gene__entrez_name__icontains=q)
        | Q(uniprot_accession__icontains=q)
        | Q(uniprot_name__icontains=q)
    )
    if q.isdigit():
        cond |= Q(gene__entrez_id=int(q))
    return cond


def _filtered_protein_qs(request):
    """
    Build the ordered Protein queryset for the browse "Proteins" mode from the
    request's filter params. Shared by ``browse_api`` and ``browse_export_api``.

    ``degree`` / ``avg_score`` are read from denormalised columns (refreshed by
    ``recompute_protein_stats``), so degree/score filters and sorting are plain
    indexed clauses. The source filter uses two ``EXISTS`` subqueries over the
    M2M through table — one per protein side — so each rides the
    ``(protein_1, score)`` / ``(protein_2, score)`` indexes instead of an
    OR-correlated subquery across a dual interaction join.
    """
    tissue_ids = [int(t) for t in request.GET.getlist("tissue") if t.isdigit()]
    source_ids = [int(s) for s in request.GET.getlist("source") if s.isdigit()]
    q = request.GET.get("q", "").strip()
    min_degree = _safe_int(request.GET.get("min_degree"))
    min_score = _safe_float(request.GET.get("min_score"))
    min_rpkm = _safe_float(request.GET.get("min_rpkm"))
    include_isoforms = request.GET.get("include_isoforms", "") in ("1", "true")

    sort_field = _BROWSE_SORT_FIELDS.get(
        request.GET.get("sort", "symbol"), "gene__entrez_name"
    )
    descending = request.GET.get("dir", "asc") == "desc"
    order = ("-" + sort_field) if descending else sort_field

    base_qs = Protein.objects.select_related("gene")
    if not include_isoforms:
        base_qs = base_qs.filter(isoform__isnull=True)

    if tissue_ids:
        base_qs = base_qs.expressed_in(tissue_ids, min_rpkm=min_rpkm)

    if source_ids:
        through = Interaction.sources.through.objects.filter(source_id__in=source_ids)
        p1 = through.filter(interaction__protein_1_id=OuterRef("pk"))
        p2 = through.filter(interaction__protein_2_id=OuterRef("pk"))
        base_qs = base_qs.filter(Exists(p1) | Exists(p2))

    if q:
        base_qs = base_qs.filter(_protein_search_q(q))

    if min_degree is not None and min_degree > 0:
        base_qs = base_qs.filter(degree__gte=min_degree)
    if min_score is not None and min_score > 0:
        base_qs = base_qs.filter(avg_score__gte=min_score)

    return base_qs.order_by(order, "pk")


def _filtered_interaction_qs(request):
    """
    Build the ordered Interaction queryset for the browse "Interactions" mode.
    Shared by ``browse_interactions_api`` and ``browse_export_api``.

    Performance notes:
      * Isoform exclusion reads the denormalised ``involves_isoform`` boolean
        (refreshed by ``recompute_interaction_flags``) — one indexed column
        instead of two ``protein_*__isoform`` anti-joins over the full table.
      * Free-text search resolves matching protein PKs in a lean subquery, then
        filters either interaction side via ``IN``.
      * Source / experiment filters use ``EXISTS`` over the M2M through table
        (indexed ``interaction_id`` lookup) — no correlated self-join.
    """
    min_score = _safe_float(request.GET.get("min_score"))
    max_score = _safe_float(request.GET.get("max_score"))
    source_ids = [int(s) for s in request.GET.getlist("source") if s.isdigit()]
    experiment_ids = [int(e) for e in request.GET.getlist("experiment") if e.isdigit()]
    q = request.GET.get("q", "").strip()
    descending = request.GET.get("dir", "desc") != "asc"
    include_isoforms = request.GET.get("include_isoforms", "") in ("1", "true")

    qs = Interaction.objects.all()
    if not include_isoforms:
        # Denormalised flag (recompute_interaction_flags) — one indexed column
        # instead of two `protein_*__isoform__isnull` anti-joins over 1.15M rows.
        qs = qs.filter(involves_isoform=False)
    if min_score is not None:
        qs = qs.filter(score__gte=min_score)
    if max_score is not None:
        qs = qs.filter(score__lte=max_score)
    if q:
        pid_qs = Protein.objects.filter(_protein_search_q(q)).values("pk")
        qs = qs.filter(
            Q(protein_1__in=Subquery(pid_qs)) | Q(protein_2__in=Subquery(pid_qs))
        )
    if source_ids:
        qs = qs.filter(
            Exists(
                Interaction.sources.through.objects.filter(
                    interaction_id=OuterRef("pk"), source_id__in=source_ids
                )
            )
        )
    if experiment_ids:
        qs = qs.filter(
            Exists(
                Interaction.experiments.through.objects.filter(
                    interaction_id=OuterRef("pk"),
                    experimenttype_id__in=experiment_ids,
                )
            )
        )

    return qs.with_proteins().order_by("-score" if descending else "score", "pk")


def _interaction_row(ix) -> dict:
    """Serialise one Interaction for the browse table. Uses prefetched M2M
    caches (``len`` not ``.count()``) to avoid per-row count queries."""
    return {
        "id": ix.pk,
        "protein_a": _protein_display(ix.protein_1),
        "protein_b": _protein_display(ix.protein_2),
        "score": round(ix.score, 4),
        "source_count": len(ix.sources.all()),
        "experiment_count": len(ix.experiments.all()),
        "detail_url": reverse("hippie_website:interaction_detail", args=[ix.pk]),
    }


@require_GET
def browse_api(request):
    """
    GET /api/browse/?offset=<int>&limit=<int>&q=<text>
                    &sort=<key>&dir=<asc|desc>
                    &tissue=<id>&tissue=<id>&source=<id>&source=<id>
                    &min_degree=<int>&min_score=<float>&min_rpkm=<float>

    Returns a single page of proteins (server-side pagination):
    {"total": <int>, "proteins": [ {id, symbol, uniprot_id, entrez_id,
                                     degree, avg_score}, ... ]}
    """
    try:
        offset = max(0, int(request.GET.get("offset", 0)))
        limit = min(200, max(1, int(request.GET.get("limit", 50))))
    except (TypeError, ValueError):
        offset, limit = 0, 50

    base_qs = _filtered_protein_qs(request)
    total = _cached_total(_count_cache_key("proteins", request), base_qs)

    proteins = [
        {
            "id": p.pk,
            "symbol": p.gene.entrez_name or p.uniprot_name,
            "uniprot_id": p.uniprot_accession,
            "entrez_id": p.gene.entrez_id or None,
            "degree": p.degree,
            "avg_score": p.avg_score,
        }
        for p in base_qs[offset : offset + limit]
    ]

    return JsonResponse({"total": total, "proteins": proteins})


@require_GET
def browse_interactions_api(request):
    """
    GET /api/browse/interactions/?offset=<int>&limit=<int>&q=<text>
                    &min_score=<float>&max_score=<float>
                    &source=<id>&source=<id>&experiment=<id>&experiment=<id>
                    &sort=score&dir=<asc|desc>

    Server-side paginated listing of the interaction table for the browse
    page's "Interactions" mode.

    Returns:
    {"total": <int>, "interactions": [ {id, protein_a, protein_b, score,
        source_count, experiment_count, detail_url}, ... ]}
    """
    try:
        offset = max(0, int(request.GET.get("offset", 0)))
        limit = min(200, max(1, int(request.GET.get("limit", 50))))
    except (TypeError, ValueError):
        offset, limit = 0, 50

    qs = _filtered_interaction_qs(request)
    total = _cached_total(_count_cache_key("interactions", request), qs)

    page = qs.prefetch_related("sources", "experiments")[offset : offset + limit]
    interactions = [_interaction_row(ix) for ix in page]

    return JsonResponse({"total": total, "interactions": interactions})


# Hard cap on rows returned by the bulk TSV export — protects the server from
# materialising the full (multi-hundred-thousand-row) interaction table.
EXPORT_CAP = 50_000


def _tsv_cell(value) -> str:
    """Render one cell, stripping characters that would break TSV structure."""
    if value is None:
        return ""
    return str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")


def _tsv_line(cells) -> str:
    return "\t".join(_tsv_cell(c) for c in cells) + "\n"


@require_GET
def browse_export_api(request):
    """
    GET /api/browse/export/?mode=proteins|interactions & <same filters as the
        matching browse list endpoint, incl. q>

    Streams a TSV of *all* rows matching the current filters (capped at
    ``EXPORT_CAP``). Reuses the shared filter helpers so the export always
    matches what the browse table shows. When the result set exceeds the cap,
    the response carries an ``X-Export-Truncated: 1`` header.
    """
    mode = request.GET.get("mode", "proteins")

    if mode == "interactions":
        qs = _filtered_interaction_qs(request)
        total = qs.count()
        header = ["Protein A", "Protein B", "Score", "Sources", "Experiments"]
        page = qs.prefetch_related("sources", "experiments")[:EXPORT_CAP]

        def rows():
            yield _tsv_line(header)
            for ix in page.iterator(chunk_size=2000):
                yield _tsv_line(
                    [
                        ix.protein_1.gene.entrez_name or ix.protein_1.uniprot_name,
                        ix.protein_2.gene.entrez_name or ix.protein_2.uniprot_name,
                        round(ix.score, 4),
                        len(ix.sources.all()),
                        len(ix.experiments.all()),
                    ]
                )
    else:
        mode = "proteins"
        qs = _filtered_protein_qs(request)
        total = qs.count()
        header = [
            "UniProt Acc",
            "Entrez Gene ID",
            "Gene Symbol",
            "Degree",
            "Avg. Score",
        ]
        page = qs[:EXPORT_CAP]

        def rows():
            yield _tsv_line(header)
            for p in page.iterator(chunk_size=2000):
                yield _tsv_line(
                    [
                        p.uniprot_accession,
                        p.gene.entrez_id or "",
                        p.gene.entrez_name or p.uniprot_name,
                        p.degree,
                        round(p.avg_score, 4) if p.avg_score is not None else "",
                    ]
                )

    response = StreamingHttpResponse(
        rows(), content_type="text/tab-separated-values; charset=utf-8"
    )
    response["Content-Disposition"] = f'attachment; filename="hippie_browse_{mode}.tsv"'
    if total > EXPORT_CAP:
        response["X-Export-Truncated"] = "1"
    return response


# Query-string keys that change pagination/ordering but NOT the matched row
# set — excluded from the count cache key so every page of one filter set
# shares a single cached total.
_COUNT_IGNORE_PARAMS = {"offset", "limit", "sort", "dir"}


def _count_cache_key(mode: str, request) -> str:
    """Stable cache key for a browse result-set total, derived from the filter
    params only (pagination/sort ignored)."""
    items = sorted(
        (k, sorted(request.GET.getlist(k)))
        for k in request.GET.keys()
        if k not in _COUNT_IGNORE_PARAMS
    )
    digest = hashlib.md5(repr(items).encode()).hexdigest()
    return f"{mode}:{digest}"


def _cached_total(cache_key: str, qs) -> int:
    """Return ``qs.count()`` memoised in the cache. Keyed under a global epoch
    so a single ``cache.set("browse:epoch", …)`` after a data import
    invalidates every cached total at once (no key enumeration needed)."""
    epoch = cache.get("browse:epoch", 0)
    full_key = f"browse:total:{epoch}:{cache_key}"
    total = cache.get(full_key)
    if total is None:
        total = qs.count()
        cache.set(full_key, total, 60 * 60)  # 1h TTL; epoch bump invalidates early
    return total


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

    Returns the data needed to populate the filter controls:
    {
        "tissues":     [{ "id": <int>, "name": "<str>" }, ...],
        "sources":     [{ "id": <int>, "name": "<str>" }, ...],
        "experiments": [{ "id": <int>, "name": "<str>" }, ...]   # interactions mode
    }
    """
    from .models import Tissue, Source, ExperimentType

    tissues = list(Tissue.objects.order_by("name").values("id", "name"))
    sources = list(Source.objects.order_by("name").values("id", "name"))
    experiments = list(ExperimentType.objects.order_by("name").values("id", "name"))
    return JsonResponse(
        {"tissues": tissues, "sources": sources, "experiments": experiments}
    )


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
                           interaction_types,
                           cross_references (+ source + species)
    Conserved species are resolved via OrthologInteraction on the gene pair.
    """
    interaction = get_object_or_404(
        Interaction.objects.with_full_detail(),
        pk=pk,
    )

    # Compute bait-prey detection stats from prefetched data (no extra queries).
    all_tests = [
        test
        for assoc in interaction.bait_prey.all()
        for test in assoc.tests_performed.all()
    ]
    bait_prey_total_tested = len(all_tests)
    bait_prey_times_observed = sum(1 for t in all_tests if t.detection)

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

    context = {
        "interaction": interaction,
        "p1": {
            "protein": p1,
            "uniprot_id": p1.uniprot_accession,
            "gene_id": p1.gene.entrez_id or None,
            "symbol": p1.gene.entrez_name or p1.uniprot_name,
        },
        "p2": {
            "protein": p2,
            "uniprot_id": p2.uniprot_accession,
            "gene_id": p2.gene.entrez_id or None,
            "symbol": p2.gene.entrez_name or p2.uniprot_name,
        },
        # All prefetched — .all() hits the cache.
        "sources": interaction.sources.all(),
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
    }
    return render(request, "hippie_website/interaction_detail.html", context)


# ---------------------------------------------------------------------------
# Non-interaction detail view
# ---------------------------------------------------------------------------


@require_GET
def noninteraction_detail_view(request, pk: int):
    """
    Show bait-prey detection evidence for a single non-interaction (Negatome).
    """
    noninteraction = get_object_or_404(
        NonInteraction.objects.select_related(
            "protein_1", "protein_1__gene", "protein_2", "protein_2__gene"
        ).prefetch_related(
            "bait_prey",
            "bait_prey__tests_performed",
        ),
        pk=pk,
    )

    all_tests = [
        test
        for assoc in noninteraction.bait_prey.all()
        for test in assoc.tests_performed.all()
    ]
    bait_prey_total_tested = len(all_tests)
    bait_prey_times_observed = sum(1 for t in all_tests if t.detection)

    p1 = noninteraction.protein_1
    p2 = noninteraction.protein_2
    context = {
        "noninteraction": noninteraction,
        "p1": {
            "protein": p1,
            "uniprot_id": p1.uniprot_accession,
            "gene_id": p1.gene.entrez_id or None,
            "symbol": p1.gene.entrez_name or p1.uniprot_name,
        },
        "p2": {
            "protein": p2,
            "uniprot_id": p2.uniprot_accession,
            "gene_id": p2.gene.entrez_id or None,
            "symbol": p2.gene.entrez_name or p2.uniprot_name,
        },
        "bait_prey_total_tested": bait_prey_total_tested,
        "bait_prey_times_observed": bait_prey_times_observed,
        # Shared with protein_pair_base.html
        "pair_score": noninteraction.score,
        "pair_label": "Non-Interaction Evidence",
        "is_noninteraction": True,
    }
    return render(request, "hippie_website/noninteraction_detail.html", context)


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
        Protein.objects.select_related("gene"),
        pk=pk,
    )

    # Count interactions using the manager for consistency.
    interaction_count = Interaction.objects.for_protein(protein.pk).count()

    context = {
        "protein": protein,
        "uniprot_id": protein.uniprot_accession,
        "gene_id": protein.gene.entrez_id or None,
        "symbol": protein.gene.entrez_name or protein.uniprot_name,
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


@require_GET
def machine_learning_view(request):
    return render(request, "hippie_website/ml.html", {})


# ---------------------------------------------------------------------------
# ML splits helpers + views
# ---------------------------------------------------------------------------


def _validate_split_params(body: dict) -> dict:
    """Coerce and validate the split/stats parameter payload.

    Keys mirror ``SplitParams`` field names exactly so the dict can be splatted
    into ``SplitParams(**params)`` (see tasks.run_split_job).
    """
    from django.core.exceptions import BadRequest

    try:
        params = {
            # interaction-level
            "min_score": float(body.get("min_score", 0.0)),
            "max_score": float(body.get("max_score", 1.0)),
            "source_ids": [int(x) for x in body.get("source_ids") or []],
            "experiment_ids": [int(x) for x in body.get("experiment_ids") or []],
            "type_ids": [int(x) for x in body.get("type_ids") or []],
            # protein-level
            "tissue_ids": [int(x) for x in body.get("tissue_ids") or []],
            "min_rpkm": float(body.get("min_rpkm", 0.0)),
            "min_degree": int(body.get("min_degree", 0)),
            "min_avg_score": float(body.get("min_avg_score", 0.0)),
            "include_isoforms": bool(body.get("include_isoforms", False)),
            # negative sampling
            "neg_ratio": float(body.get("neg_ratio", 1.0)),
            "seed": int(body.get("seed", 78539105873)),
        }
    except (ValueError, TypeError) as exc:
        raise BadRequest(str(exc))
    if not 0.0 <= params["min_score"] <= 1.0:
        raise BadRequest("min_score must be between 0.0 and 1.0")
    if not 0.0 <= params["max_score"] <= 1.0:
        raise BadRequest("max_score must be between 0.0 and 1.0")
    if params["max_score"] < params["min_score"]:
        raise BadRequest("max_score must be >= min_score")
    if not 0.0 <= params["min_avg_score"] <= 1.0:
        raise BadRequest("min_avg_score must be between 0.0 and 1.0")
    if params["min_degree"] < 0:
        raise BadRequest("min_degree must be >= 0")
    if params["min_rpkm"] < 0:
        raise BadRequest("min_rpkm must be >= 0")
    if not 0.1 <= params["neg_ratio"] <= 10.0:
        raise BadRequest("neg_ratio must be between 0.1 and 10.0")
    return params


def _ml_filter_meta() -> dict:
    """Filter option lists for the editable ML-splits controls."""
    from .models import Source, ExperimentType

    return {
        "tissues": list(Tissue.objects.order_by("name").values("id", "name")),
        "sources": list(Source.objects.order_by("name").values("id", "name")),
        "experiments": list(
            ExperimentType.objects.order_by("name").values("id", "name")
        ),
        "interaction_types": list(
            InteractionType.objects.order_by("name").values("id", "name")
        ),
    }


@require_GET
def ml_splits_view(request):
    """Standalone ML-splits page. All filters are editable on the page; any
    query params (handed off from either Browse tab) seed the initial values."""

    def _float(name):
        return request.GET.get(name, "")

    initial = {
        # interaction-level
        "min_score": request.GET.get("min_score", ""),
        "max_score": request.GET.get("max_score", ""),
        "source_ids": [int(s) for s in request.GET.getlist("source") if s.isdigit()],
        "experiment_ids": [
            int(e) for e in request.GET.getlist("experiment") if e.isdigit()
        ],
        "type_ids": [int(t) for t in request.GET.getlist("type") if t.isdigit()],
        # protein-level
        "tissue_ids": [int(t) for t in request.GET.getlist("tissue") if t.isdigit()],
        "min_rpkm": _float("min_rpkm"),
        "min_degree": request.GET.get("min_degree", ""),
        "min_avg_score": _float("min_avg_score"),
        "include_isoforms": request.GET.get("include_isoforms", "") in ("1", "true"),
    }
    return render(
        request,
        "hippie_website/ml_splits.html",
        {
            "meta_json": json.dumps(_ml_filter_meta()),
            "initial_json": json.dumps(initial),
        },
    )


@require_POST
def browse_splits_create(request):
    body = json.loads(request.body)
    params = _validate_split_params(body)  # raises 400 on bad input
    job = SplitJob.objects.create(params=params)
    run_split_job.delay(str(job.id))
    return JsonResponse({"job_id": str(job.id), "status": job.status}, status=202)


def _protein_filtered_qs(params):
    """Protein queryset after the protein-level filters (for the stats box)."""
    pqs = Protein.objects.all()
    if not params.include_isoforms:
        pqs = pqs.filter(isoform__isnull=True)
    if params.tissue_ids:
        pqs = pqs.expressed_in(
            list(params.tissue_ids),
            min_rpkm=params.min_rpkm if params.min_rpkm > 0 else None,
        )
    if params.min_degree > 0:
        pqs = pqs.filter(degree__gte=params.min_degree)
    if params.min_avg_score > 0:
        pqs = pqs.filter(avg_score__gte=params.min_avg_score)
    return pqs


def _protein_stats(params) -> dict:
    import statistics

    from .models import GeneTissue

    pqs = _protein_filtered_qs(params)

    degrees: list[int] = []
    scores: list[float] = []
    n_isolated = 0
    # pk in the row keeps tissue-join duplicates from collapsing distinct
    # proteins that happen to share (degree, avg_score).
    for _pk, deg, avg in (
        pqs.values_list("pk", "degree", "avg_score")
        .distinct()
        .iterator(chunk_size=10_000)
    ):
        degrees.append(deg)
        if deg == 0:
            n_isolated += 1
        if avg is not None:
            scores.append(avg)

    n_isoforms = (
        0
        if not params.include_isoforms
        else pqs.filter(isoform__isnull=False).values("pk").distinct().count()
    )
    tissue_coverage = (
        GeneTissue.objects.filter(gene__proteins__in=pqs)
        .values("tissue_id")
        .distinct()
        .count()
    )

    return {
        "n_proteins": len(degrees),
        "median_degree": statistics.median(degrees) if degrees else 0,
        "median_avg_score": round(statistics.median(scores), 4) if scores else None,
        "n_isolated": n_isolated,
        "tissue_coverage": tissue_coverage,
        "n_isoforms": n_isoforms,
    }


# Degree-histogram buckets: (label, lower, upper-inclusive). None upper = open.
_DEGREE_BUCKETS = [
    ("0", 0, 0),
    ("1", 1, 1),
    ("2", 2, 2),
    ("3–5", 3, 5),
    ("6–10", 6, 10),
    ("11–25", 11, 25),
    ("26–50", 26, 50),
    ("51–100", 51, 100),
    ("100+", 101, None),
]


def _bucket_degrees(degree_by_node: dict) -> list:
    counts = [0] * len(_DEGREE_BUCKETS)
    for d in degree_by_node.values():
        for i, (_label, lo, hi) in enumerate(_DEGREE_BUCKETS):
            if d >= lo and (hi is None or d <= hi):
                counts[i] += 1
                break
    return [{"label": _DEGREE_BUCKETS[i][0], "count": c} for i, c in enumerate(counts)]


def _interaction_stats(iqs) -> dict:
    from .models import Interaction

    degree_by_node: dict[int, int] = {}
    score_hist = [0] * 10  # display: 10 bins of width 0.1
    score_fine = [0] * 100  # internal: for a memory-safe median
    n = 0
    for p1, p2, score in iqs.values_list(
        "protein_1_id", "protein_2_id", "score"
    ).iterator(chunk_size=10_000):
        n += 1
        degree_by_node[p1] = degree_by_node.get(p1, 0) + 1
        degree_by_node[p2] = degree_by_node.get(p2, 0) + 1
        score_hist[min(int(score * 10), 9)] += 1
        score_fine[min(int(score * 100), 99)] += 1

    # Median score from the fine histogram (O(1) memory).
    median_score = None
    if n:
        half, cum = n / 2, 0
        for i, c in enumerate(score_fine):
            cum += c
            if cum >= half:
                median_score = round((i + 0.5) / 100, 4)
                break

    n_sources = (
        Interaction.sources.through.objects.filter(interaction__in=iqs)
        .values("source_id")
        .distinct()
        .count()
    )
    n_experiments = (
        Interaction.experiments.through.objects.filter(interaction__in=iqs)
        .values("experimenttype_id")
        .distinct()
        .count()
    )

    return {
        "n_interactions": n,
        "degree_histogram": _bucket_degrees(degree_by_node),
        # Label = lower bin edge (e.g. "0.3" = bucket [0.3, 0.4)); short enough
        # to read along a 10-bin X axis.
        "score_histogram": [
            {"label": f"{i / 10:.1f}", "count": c} for i, c in enumerate(score_hist)
        ],
        "median_score": median_score,
        "n_sources": n_sources,
        "n_experiments": n_experiments,
    }


@require_POST
def browse_splits_stats(request):
    """POST the split filter payload, get on-demand protein + interaction
    statistics for the filter-preview boxes. Same param shape as
    ``browse_splits_create``."""
    from .services.generate_splits import SplitParams, build_interaction_queryset

    body = json.loads(request.body)
    params = SplitParams(**_validate_split_params(body))
    return JsonResponse(
        {
            "protein": _protein_stats(params),
            "interaction": _interaction_stats(build_interaction_queryset(params)),
        }
    )


@require_GET
def browse_splits_status(request, job_id):
    job = get_object_or_404(SplitJob, pk=job_id)
    return JsonResponse(
        {
            "status": job.status,
            "step": job.step,
            "progress": job.progress,
            "summary": job.summary,
            "error": job.error or None,
            "download_url": (
                reverse("hippie_website:browse_splits_download", args=[job.id])
                if job.status == "DONE"
                else None
            ),
        }
    )


@require_GET
def browse_splits_download(request, job_id):
    job = get_object_or_404(SplitJob, pk=job_id, status="DONE")
    return FileResponse(
        open(job.zip_path, "rb"),
        as_attachment=True,
        filename=f"hippie_splits_{job_id}.zip",
    )  # ---------------------------------------------------------------------------
