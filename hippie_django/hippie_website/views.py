"""
Views for the HIPPIE protein query interface.

Provides:
  - protein_query_view        : landing page (renders the React shell)
  - protein_query_api         : JSON endpoint consumed by the React table
  - interaction_detail_view   : single interaction evidence page

All database access goes through the custom managers defined in managers.py:
  - Protein.objects.resolve(identifier)       → ProteinQuerySet
  - Interaction.objects.for_protein(pk)       → InteractionQuerySet
  - Interaction.objects.with_proteins()       → adds select_related + prefetch
  - Interaction.objects.with_full_detail()    → full prefetch for detail page
"""

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from django.conf import settings
from django.core.cache import cache
from django.db.models import (
    CharField,
    Exists,
    F,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    Value,
)
from django.db.models.functions import Coalesce, NullIf
from django.http import FileResponse, Http404, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from .models import (
    Interaction,
    Isoform,
    OrthologInteraction,
    Protein,
    Tissue,
    NonInteraction,
    SplitJob,
    InteractionType,
    ProteinSynonym,
    GeneSynonym,
)

from .tasks import run_split_job
from .query_filters import (
    apply_interaction_level_filters,
    apply_protein_level_filters,
    canonical_or_queried_q,
    group_by_side,
    isoform_only_q,
    parse_isoform_mode,
)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _parse_json_body(request):
    """Parse a JSON request body. Returns ``(body, None)`` on success or
    ``(None, <400 JsonResponse>)`` when the body is not valid JSON, so every
    POST endpoint rejects malformed input the same way."""
    try:
        return json.loads(request.body), None
    except json.JSONDecodeError:
        return None, JsonResponse({"error": "Invalid JSON body."}, status=400)


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
        "is_reviewed": protein.is_reviewed,
        # isoform_uid is set when this protein is an isoform; None for canonical.
        "isoform_uniprot_id": isoform_uid
        if isoform_uid is not None
        else getattr(protein, "isoform_uniprot_id", None),
    }


# Max distinct identifiers accepted by the single-protein search endpoints
# (Protein Query, Browse). Mirrors BATCH_LIMIT=200 on the interaction endpoint.
MAX_QUERY_PROTEINS = 50
_IDENT_SPLIT = re.compile(r"[\s,;]+")


def _split_identifiers(raw: str) -> list[str]:
    """
    Split a raw search string into identifiers on comma, whitespace (space,
    tab, newline) or semicolon. Trims each token and drops empties, preserving
    input order while removing duplicates.
    """
    seen: set[str] = set()
    out: list[str] = []
    for tok in _IDENT_SPLIT.split(raw.strip()):
        tok = tok.strip()
        if tok and tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def _protein_ids_from_raw(raw: str) -> tuple[list[int], list[str]]:
    """
    Resolve a delimited string of identifiers (comma/whitespace/semicolon) to
    Protein PKs. Returns (resolved_pks, unresolved_identifiers).
    """
    protein_ids: list[int] = []
    unresolved: list[str] = []
    seen: set[int] = set()
    for ident in _split_identifiers(raw):
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


# ---------------------------------------------------------------------------
# Shared query filters (Protein Query + Interaction Query — Batch 3)
#
# One filter contract shared by the two React query pages so the single
# FilterBox component emits identical params everywhere. Interaction-level
# filters (score / source / experiment / interaction-type) are applied to an
# Interaction queryset (or matched against a prefetched Interaction); protein-
# level filters (degree / avg-score / reviewed / tissue) are checked per
# Protein in Python — query-page result sets are small (one protein's partners,
# or a user-supplied pair list), so no full-table scan is involved.
# ---------------------------------------------------------------------------


@dataclass
class CommonFilters:
    show: str = "interactions"  # interactions | noninteractions | both
    isoform_mode: str = "general"  # general | isoforms | both
    min_score: float | None = None
    max_score: float | None = None
    source_ids: list[int] = field(default_factory=list)
    experiment_ids: list[int] = field(default_factory=list)
    interaction_type_ids: list[int] = field(default_factory=list)
    tissue_ids: list[int] = field(default_factory=list)
    min_rpkm: float | None = None
    min_degree: int | None = None
    min_avg_score: float | None = None
    reviewed: str = "both"  # both | reviewed | unreviewed

    @property
    def has_source_like(self) -> bool:
        """True when a filter is active that a NonInteraction can never satisfy
        (non-interactions carry no sources / experiments / interaction types)."""
        return bool(self.source_ids or self.experiment_ids or self.interaction_type_ids)

    @property
    def has_protein_level(self) -> bool:
        return (
            self.min_degree is not None
            or self.min_avg_score is not None
            or self.reviewed != "both"
            or bool(self.tissue_ids)
        )


def _int_id_list(values) -> list[int]:
    return [int(v) for v in values if str(v).isdigit()]


def _build_common_filters(get_scalar, get_list) -> CommonFilters:
    show = get_scalar("show", "interactions")
    if show not in ("interactions", "noninteractions", "both"):
        show = "interactions"
    reviewed = get_scalar("reviewed", "both")
    if reviewed not in ("both", "reviewed", "unreviewed"):
        reviewed = "both"
    return CommonFilters(
        show=show,
        isoform_mode=parse_isoform_mode(get_scalar("isoform_mode")),
        min_score=_safe_float(get_scalar("min_score")),
        max_score=_safe_float(get_scalar("max_score")),
        source_ids=_int_id_list(get_list("source")),
        experiment_ids=_int_id_list(get_list("experiment")),
        interaction_type_ids=_int_id_list(get_list("interaction_type")),
        tissue_ids=_int_id_list(get_list("tissue")),
        min_rpkm=_safe_float(get_scalar("min_rpkm")),
        min_degree=_safe_int(get_scalar("min_degree")),
        min_avg_score=_safe_float(get_scalar("min_avg_score")),
        reviewed=reviewed,
    )


def _common_filters_from_get(get) -> CommonFilters:
    return _build_common_filters(get.get, get.getlist)


def _common_filters_from_body(body: dict) -> CommonFilters:
    def get_scalar(key: str, default=None):
        val = body.get(key, default)
        if isinstance(val, list):
            return val[0] if val else default
        return val

    def get_list(key: str) -> list:
        val = body.get(key, [])
        if isinstance(val, list):
            return val
        return [val] if val not in (None, "") else []

    return _build_common_filters(get_scalar, get_list)


def _apply_interaction_level_filters(qs, f: CommonFilters):
    """Apply the CommonFilters interaction-level gates (score / source /
    experiment / interaction-type) via the shared query_filters helper."""
    return apply_interaction_level_filters(
        qs,
        min_score=f.min_score,
        max_score=f.max_score,
        source_ids=f.source_ids,
        experiment_ids=f.experiment_ids,
        type_ids=f.interaction_type_ids,
    )


def _interaction_matches(interaction, f: CommonFilters) -> bool:
    """Check a single (prefetched) Interaction against the interaction-level
    filters. Requires sources / experiments / interaction_types prefetched."""
    if f.min_score is not None and interaction.score < f.min_score:
        return False
    if f.max_score is not None and interaction.score > f.max_score:
        return False
    if f.source_ids:
        wanted = set(f.source_ids)
        if not any(s.pk in wanted for s in interaction.sources.all()):
            return False
    if f.experiment_ids:
        wanted = set(f.experiment_ids)
        if not any(e.pk in wanted for e in interaction.experiments.all()):
            return False
    if f.interaction_type_ids:
        wanted = set(f.interaction_type_ids)
        if not any(t.pk in wanted for t in interaction.interaction_types.all()):
            return False
    return True


def _tissue_pk_set(f: CommonFilters) -> set[int] | None:
    """PKs of proteins expressed in any selected tissue (≥ min_rpkm), or None
    when no tissue filter is active. Computed once per request."""
    if not f.tissue_ids:
        return None
    return set(
        Protein.objects.expressed_in(f.tissue_ids, min_rpkm=f.min_rpkm).values_list(
            "pk", flat=True
        )
    )


def _protein_passes(
    protein: Protein, f: CommonFilters, tissue_pks: set[int] | None
) -> bool:
    """Check one Protein against the protein-level filters."""
    if f.min_degree is not None and (protein.degree or 0) < f.min_degree:
        return False
    if f.min_avg_score is not None and (
        protein.avg_score is None or protein.avg_score < f.min_avg_score
    ):
        return False
    if f.reviewed == "reviewed" and not protein.is_reviewed:
        return False
    if f.reviewed == "unreviewed" and protein.is_reviewed:
        return False
    if tissue_pks is not None and protein.pk not in tissue_pks:
        return False
    return True


def _interaction_edge_qs(protein_pks, f: CommonFilters):
    """Ordered, filter-applied Interaction queryset for the query / network
    pages: every interaction touching a queried protein, gated by the isoform
    mode (general: canonical-or-queried; isoforms: at least one isoform
    endpoint; both: no isoform filter), plus the interaction-level filters.
    Callers keep their own per-row protein-level filtering."""
    qs = (
        Interaction.objects.for_proteins(protein_pks)
        .with_proteins()
        .prefetch_related("sources", "experiments")
        .order_by("-score")
    )
    if f.isoform_mode == "general":
        qs = qs.filter(canonical_or_queried_q(protein_pks))
    elif f.isoform_mode == "isoforms":
        qs = qs.filter(isoform_only_q())
    return _apply_interaction_level_filters(qs, f)


def _noninteraction_edge_qs(protein_pks, f: CommonFilters):
    """Ordered NonInteraction queryset for the query / network pages: the
    isoform-mode gate (see _interaction_edge_qs) plus the score range.
    Non-interactions carry no source / experiment / type evidence, so those
    filters never apply (callers gate the whole leg out when a source-like
    filter is active)."""
    qs = (
        NonInteraction.objects.filter(
            Q(protein_1_id__in=protein_pks) | Q(protein_2_id__in=protein_pks)
        )
        .select_related("protein_1", "protein_1__gene", "protein_2", "protein_2__gene")
        .order_by("-score")
    )
    if f.isoform_mode == "general":
        qs = qs.filter(canonical_or_queried_q(protein_pks))
    elif f.isoform_mode == "isoforms":
        qs = qs.filter(isoform_only_q())
    if f.min_score is not None:
        qs = qs.filter(score__gte=f.min_score)
    if f.max_score is not None:
        qs = qs.filter(score__lte=f.max_score)
    return qs


@require_GET
def protein_query_api(request):
    """
    GET /api/query/?q=<identifiers>[&isoform_mode=general|isoforms|both]

    ``q`` may contain up to MAX_QUERY_PROTEINS identifiers separated by comma,
    space or tab. Each is resolved independently via Protein.objects.resolve()
    (gene symbol, UniProt accession/entry-name, Entrez id, isoform accession,
    or Ensembl id via synonyms). Interactions for all resolved proteins are
    fetched via Interaction.objects.for_proteins() and returned as one list;
    an edge touching two queried proteins appears once.

    Identifiers that resolve to nothing are collected in ``unresolved`` and the
    request still succeeds with results for the ones that matched. The request
    fails (400) only when more than MAX_QUERY_PROTEINS identifiers are supplied.

    When isoform_mode is "isoforms" or "both", every canonical resolved protein
    is also expanded to its known isoforms, and all results are returned together.

    Response shape:
    {
        "query_proteins":    [ { id, name, uniprot_id, gene_id, symbol, isoform_uniprot_id }, ... ],
        "isoforms_included": <bool>,
        "expanded_proteins": [ ...same shape... ],   // isoforms added to the query
        "unresolved":        [ "<identifier>", ... ],  // inputs that matched nothing
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
    q = request.GET.get("q", "")
    f = _common_filters_from_get(request.GET)
    expand_isoforms = f.isoform_mode in ("isoforms", "both")
    show = f.show
    tissue_pks = _tissue_pk_set(f)

    tokens = _split_identifiers(q)
    if not tokens:
        return JsonResponse(
            {
                "error": "No query provided.",
                "interactions": [],
                "query_proteins": [],
                "unresolved": [],
            }
        )
    if len(tokens) > MAX_QUERY_PROTEINS:
        return JsonResponse(
            {
                "error": (
                    f"Too many proteins: {len(tokens)} "
                    f"(max {MAX_QUERY_PROTEINS} per request)."
                ),
                "interactions": [],
                "query_proteins": [],
                "unresolved": [],
            },
            status=400,
        )

    # ── Resolve each identifier → Protein (order-preserving, deduped) ──
    resolved: list = []
    resolved_pks: set[int] = set()
    unresolved: list[str] = []
    for tok in tokens:
        protein = Protein.objects.resolve(tok).select_related("gene").first()
        if protein is None:
            unresolved.append(tok)
        elif protein.pk not in resolved_pks:
            resolved_pks.add(protein.pk)
            resolved.append(protein)

    if not resolved:
        return JsonResponse(
            {
                "error": f"No proteins found for: {', '.join(unresolved)}.",
                "interactions": [],
                "query_proteins": [],
                "unresolved": unresolved,
            }
        )

    # ── Build the combined PK set and isoform-accession display map ────
    # A queried identifier may itself be an isoform (resolve() annotates
    # ``isoform_uniprot_id``); when isoform_mode expands, each canonical seed
    # also contributes its known isoforms. All are unioned into protein_pks.
    protein_pks: list[int] = [p.pk for p in resolved]
    isoform_uid_map: dict[int, str] = {}
    for p in resolved:
        uid = getattr(p, "isoform_uniprot_id", None)
        if uid:
            isoform_uid_map[p.pk] = uid

    isoforms: list = []
    if expand_isoforms:
        seen_iso: set[int] = set(resolved_pks)
        for p in resolved:
            for iso in _get_isoforms(p.pk):
                if iso.pk not in seen_iso:
                    seen_iso.add(iso.pk)
                    isoforms.append(iso)
                    protein_pks.append(iso.pk)
                    isoform_uid_map[iso.pk] = iso.uniprot_accession

    protein_pks_set = set(protein_pks)

    # ── Fetch interactions and/or non-interactions -──────────────────
    # for_proteins() handles a single-element list the same as for_protein().

    results = []
    if show in ("interactions", "both"):
        interactions_qs = _interaction_edge_qs(protein_pks, f)
        for interaction in interactions_qs:
            if interaction.protein_1_id in protein_pks_set:
                query_side, partner = interaction.protein_1, interaction.protein_2
            else:
                query_side, partner = interaction.protein_2, interaction.protein_1
            # Protein-level filters apply to the partner (B) side.
            if not _protein_passes(partner, f, tissue_pks):
                continue
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

    if show in ("noninteractions", "both") and not f.has_source_like:
        noninteractions_qs = _noninteraction_edge_qs(protein_pks, f)
        for ni in noninteractions_qs:
            if ni.protein_1_id in protein_pks_set:
                query_side, partner = ni.protein_1, ni.protein_2
            else:
                query_side, partner = ni.protein_2, ni.protein_1
            # Protein-level filters apply to the partner (B) side.
            if not _protein_passes(partner, f, tissue_pks):
                continue
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
            "query_proteins": [
                _protein_display(p, isoform_uid_map.get(p.pk)) for p in resolved
            ],
            "isoforms_included": expand_isoforms,
            "expanded_proteins": [_protein_display(iso) for iso in isoforms],
            "unresolved": unresolved,
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
        "isoform_mode": "general" | "isoforms" | "both"   // optional, default "general"
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
    body, err = _parse_json_body(request)
    if err:
        return err

    raw_pairs = body.get("pairs", [])
    f = _common_filters_from_body(body)
    expand_isoforms = f.isoform_mode in ("isoforms", "both")
    show = f.show
    tissue_pks = _tissue_pk_set(f)

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

        if expand_isoforms:
            # Isoform expansion only applies to the Interaction table.
            int_rows: list[dict] = []
            if show in ("interactions", "both"):
                int_rows = _resolve_interaction_pair_with_isoforms(
                    input_a,
                    input_b,
                    input_order,
                    isoform_cache,
                    f,
                    tissue_pks,
                    isoform_mode=f.isoform_mode,
                )
            nonint_rows: list[dict] = []
            if show in ("noninteractions", "both"):
                nr = _resolve_noninteraction_pair(
                    input_a, input_b, input_order, f, tissue_pks
                )
                if nr["score"] >= 0:
                    nonint_rows = [nr]
            rows = int_rows + nonint_rows
            # A found row (interaction OR non-interaction) supersedes the
            # not-found (score -1) fallback the isoform resolver emits when no
            # interaction combo matches — keeps exactly one row per input pair
            # (mirrors the non-isoform "both" branch below).
            found = [r for r in rows if r["score"] >= 0]
            if found:
                rows = found
            elif not rows:
                # Nothing found in either table — return a single not-found row.
                if show == "noninteractions":
                    rows = [
                        _resolve_noninteraction_pair(
                            input_a, input_b, input_order, f, tissue_pks
                        )
                    ]
                else:
                    rows = [
                        _resolve_interaction_pair(
                            input_a, input_b, input_order, f, tissue_pks
                        )
                    ]
        else:
            if show == "interactions":
                rows = [
                    _resolve_interaction_pair(
                        input_a, input_b, input_order, f, tissue_pks
                    )
                ]
            elif show == "noninteractions":
                rows = [
                    _resolve_noninteraction_pair(
                        input_a, input_b, input_order, f, tissue_pks
                    )
                ]
            else:  # both
                int_row = _resolve_interaction_pair(
                    input_a, input_b, input_order, f, tissue_pks
                )
                nonint_row = _resolve_noninteraction_pair(
                    input_a, input_b, input_order, f, tissue_pks
                )
                found = [r for r in [int_row, nonint_row] if r["score"] >= 0]
                rows = found if found else [int_row]

        results.extend(rows)
    return JsonResponse({"results": results})


def _pair_not_found(
    input_a: str,
    input_b: str,
    input_order: int,
    *,
    is_noninteraction: bool,
    ua: dict | None = None,
    ub: dict | None = None,
) -> dict:
    """Build the not-found (score -1) row for an input pair.

    When ``ua`` / ``ub`` (the ``_protein_display`` dicts) are given the proteins
    resolved but no (non-)interaction was recorded / it failed the filters;
    otherwise an identifier was unknown. Non-interactions carry no evidence
    counts (``None``); interactions report ``0``.
    """
    counts = None if is_noninteraction else 0
    return {
        "input_order": input_order,
        "input_a": input_a,
        "input_b": input_b,
        "symbol_a": ua["symbol"] if ua else input_a,
        "symbol_b": ub["symbol"] if ub else input_b,
        "uniprot_a": ua["uniprot_id"] if ua else "",
        "uniprot_b": ub["uniprot_id"] if ub else "",
        "isoform_uniprot_a": ua["isoform_uniprot_id"] if ua else None,
        "isoform_uniprot_b": ub["isoform_uniprot_id"] if ub else None,
        "score": -1.0,
        "source_count": counts,
        "experiment_count": counts,
        "entrez_a": ua["gene_id"] if ua else None,
        "entrez_b": ub["gene_id"] if ub else None,
        "is_reviewed_a": ua["is_reviewed"] if ua else None,
        "is_reviewed_b": ub["is_reviewed"] if ub else None,
        "is_noninteraction": is_noninteraction,
        "interaction_id": None,
        "detail_url": "",
    }


def _pair_row(
    ua: dict,
    ub: dict,
    *,
    input_a: str,
    input_b: str,
    input_order: int,
    score: float,
    source_count,
    experiment_count,
    obj_pk: int,
    is_noninteraction: bool,
) -> dict:
    """Build a found-pair result row from two ``_protein_display`` dicts."""
    route = (
        "hippie_website:noninteraction_detail"
        if is_noninteraction
        else "hippie_website:interaction_detail"
    )
    return {
        "input_order": input_order,
        "input_a": input_a,
        "input_b": input_b,
        "symbol_a": ua["symbol"],
        "symbol_b": ub["symbol"],
        "uniprot_a": ua["uniprot_id"],
        "uniprot_b": ub["uniprot_id"],
        "entrez_a": ua["gene_id"],
        "entrez_b": ub["gene_id"],
        "isoform_uniprot_a": ua["isoform_uniprot_id"],
        "isoform_uniprot_b": ub["isoform_uniprot_id"],
        "is_reviewed_a": ua["is_reviewed"],
        "is_reviewed_b": ub["is_reviewed"],
        "score": round(score, 4),
        "source_count": source_count,
        "experiment_count": experiment_count,
        "interaction_id": obj_pk,
        "is_noninteraction": is_noninteraction,
        "detail_url": reverse(route, args=[obj_pk]),
    }


def _resolve_pair(
    input_a: str,
    input_b: str,
    input_order: int,
    f: CommonFilters | None = None,
    tissue_pks: set[int] | None = None,
    *,
    is_noninteraction: bool,
) -> dict:
    """
    Resolve two identifiers to proteins and look up their (non-)interaction,
    returning a result row. A score of -1.0 signals "not found" (either protein
    unknown, or no record between them / it failed the active filters).

    A found (non-)interaction that fails the active filters is reported as
    not-found rather than dropped, so every input pair keeps exactly one row.
    """
    protein_a = Protein.objects.resolve(input_a).select_related("gene").first()
    protein_b = Protein.objects.resolve(input_b).select_related("gene").first()

    if protein_a is None or protein_b is None:
        return _pair_not_found(
            input_a, input_b, input_order, is_noninteraction=is_noninteraction
        )

    p1, p2 = (
        (protein_a, protein_b)
        if protein_a.pk <= protein_b.pk
        else (protein_b, protein_a)
    )
    ua = _protein_display(protein_a)
    ub = _protein_display(protein_b)

    def _nf() -> dict:
        return _pair_not_found(
            input_a,
            input_b,
            input_order,
            is_noninteraction=is_noninteraction,
            ua=ua,
            ub=ub,
        )

    if is_noninteraction:
        try:
            obj = NonInteraction.objects.get(protein_1=p1, protein_2=p2)
        except NonInteraction.DoesNotExist:
            return _nf()
        # Non-interactions carry no sources / experiments / interaction-types, so
        # any source-like filter excludes them; score + protein filters still apply.
        if f is not None and (
            f.has_source_like
            or (f.min_score is not None and obj.score < f.min_score)
            or (f.max_score is not None and obj.score > f.max_score)
            or not _protein_passes(protein_a, f, tissue_pks)
            or not _protein_passes(protein_b, f, tissue_pks)
        ):
            return _nf()
        return _pair_row(
            ua,
            ub,
            input_a=input_a,
            input_b=input_b,
            input_order=input_order,
            score=obj.score,
            source_count=None,
            experiment_count=None,
            obj_pk=obj.pk,
            is_noninteraction=True,
        )

    try:
        obj = (
            Interaction.objects.with_proteins()
            .prefetch_related("sources", "experiments", "interaction_types")
            .get(protein_1=p1, protein_2=p2)
        )
    except Interaction.DoesNotExist:
        return _nf()
    if f is not None and (
        not _interaction_matches(obj, f)
        or not _protein_passes(protein_a, f, tissue_pks)
        or not _protein_passes(protein_b, f, tissue_pks)
    ):
        return _nf()
    return _pair_row(
        ua,
        ub,
        input_a=input_a,
        input_b=input_b,
        input_order=input_order,
        score=obj.score,
        source_count=obj.sources.all().count(),
        experiment_count=obj.experiments.all().count(),
        obj_pk=obj.pk,
        is_noninteraction=False,
    )


def _resolve_interaction_pair(
    input_a: str,
    input_b: str,
    input_order: int,
    f: CommonFilters | None = None,
    tissue_pks: set[int] | None = None,
) -> dict:
    """Resolve a pair against the Interaction table (see _resolve_pair)."""
    return _resolve_pair(
        input_a, input_b, input_order, f, tissue_pks, is_noninteraction=False
    )


def _resolve_noninteraction_pair(
    input_a: str,
    input_b: str,
    input_order: int,
    f: CommonFilters | None = None,
    tissue_pks: set[int] | None = None,
) -> dict:
    """Resolve a pair against the NonInteraction table (see _resolve_pair)."""
    return _resolve_pair(
        input_a, input_b, input_order, f, tissue_pks, is_noninteraction=True
    )


def _resolve_interaction_pair_with_isoforms(
    input_a: str,
    input_b: str,
    input_order: int,
    isoform_cache: dict,
    f: CommonFilters | None = None,
    tissue_pks: set[int] | None = None,
    isoform_mode: str = "both",
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
      • In "isoforms" mode, the pure canonical×canonical combo (the original,
        unsubstituted pair) is dropped — that combo belongs to "general" mode.

    isoform_cache: a per-request dict[protein_pk -> list[Isoform]] to avoid
    repeated DB lookups when the same protein appears in multiple pairs.
    """
    protein_a = Protein.objects.resolve(input_a).select_related("gene").first()
    protein_b = Protein.objects.resolve(input_b).select_related("gene").first()

    if protein_a is None or protein_b is None:
        return [_resolve_interaction_pair(input_a, input_b, input_order, f, tissue_pks)]

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

    if isoform_mode == "isoforms":
        canonical_pairs = {
            key: origin
            for key, origin in canonical_pairs.items()
            if origin != (protein_a.pk, protein_b.pk)
        }

    if not canonical_pairs:
        if isoform_mode == "isoforms":
            return [
                _pair_not_found(
                    input_a,
                    input_b,
                    input_order,
                    is_noninteraction=False,
                    ua=_protein_display(protein_a),
                    ub=_protein_display(protein_b),
                )
            ]
        return [_resolve_interaction_pair(input_a, input_b, input_order, f, tissue_pks)]

    # Fetch all interactions in a single query --------------------------------
    q = Q()
    for p1_pk, p2_pk in canonical_pairs:
        q |= Q(protein_1_id=p1_pk, protein_2_id=p2_pk)

    interactions_qs = (
        Interaction.objects.with_proteins()
        .prefetch_related("sources", "experiments", "interaction_types")
        .filter(q)
    )
    if f is not None:
        interactions_qs = _apply_interaction_level_filters(interactions_qs, f)
    found_interactions: dict[tuple[int, int], Interaction] = {
        (i.protein_1_id, i.protein_2_id): i for i in interactions_qs
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

        # Protein-level filters apply to both sides of every isoform combination.
        if f is not None and not (
            _protein_passes(pa, f, tissue_pks) and _protein_passes(pb, f, tissue_pks)
        ):
            continue

        ua = _protein_display(pa, isoform_uid_map.get(pa_pk))
        ub = _protein_display(pb, isoform_uid_map.get(pb_pk))
        found_results.append(
            _pair_row(
                ua,
                ub,
                input_a=input_a,
                input_b=input_b,
                input_order=input_order,
                score=interaction.score,
                source_count=interaction.sources.all().count(),
                experiment_count=interaction.experiments.all().count(),
                obj_pk=interaction.pk,
                is_noninteraction=False,
            )
        )

    # If no isoform combination found anything, show original pair as not-found.
    if not found_results:
        if isoform_mode == "isoforms":
            return [
                _pair_not_found(
                    input_a,
                    input_b,
                    input_order,
                    is_noninteraction=False,
                    ua=_protein_display(protein_a),
                    ub=_protein_display(protein_b),
                )
            ]
        return [_resolve_interaction_pair(input_a, input_b, input_order, f, tissue_pks)]

    return found_results


# ---------------------------------------------------------------------------
# Network query
# ---------------------------------------------------------------------------


# Seed / edge caps. Layer semantics are fixed to first-shell ("set vs. HIPPIE")
# — every edge touching a seed — since the standalone layer toggle was retired
# when Network Query moved to the shared React FilterBox.
MAX_SEED_PROTEINS = 1_000  # cap on resolved seed identifiers
MAX_NETWORK_EDGES = 5_000  # cap on returned edges (protects the browser)


@require_GET
def network_query_view(request):
    """Render the React shell; all data loads via the JSON API."""
    return render(request, "hippie_website/network_query.html")


@require_POST
def network_query_api(request):
    """
    POST /api/network/

    Request body (JSON):
        {
            "proteins": "<newline/space-separated seed identifiers>",
            ...shared FilterBox params (show, min_score, max_score, source[],
               experiment[], interaction_type[], tissue[], min_rpkm,
               min_degree, min_avg_score, reviewed, isoform_mode)
        }

    Builds the first-shell sub-network around the seed proteins — every edge
    touching a seed ("set vs. HIPPIE") — honouring the full shared filter set.
    Non-interactions are included when ``show`` is noninteractions / both.

    Response (JSON):
        {
            "node_count":   <int>,
            "edge_count":   <int>,
            "interactions": [ { id, a, b, score, source_count,
                                experiment_count, is_noninteraction,
                                seed_interaction, detail_url }, ... ],
            "seed_ids":     [<protein pk in the query set>, ...],
            "unresolved":   [<raw identifier>, ...],
            "truncated":    <bool>,
            "total_edges":  <int>,
            "error":        null | "<message>"
        }

    ``a`` / ``b`` are _protein_display() dicts for protein_1 / protein_2.
    """
    body, err = _parse_json_body(request)
    if err:
        return err

    f = _common_filters_from_body(body)
    result = _run_network_query(str(body.get("proteins", "")), f)
    if result.get("error"):
        return JsonResponse(result, status=400)
    return JsonResponse(result)


def _run_network_query(raw_proteins: str, f: CommonFilters) -> dict:
    """Assemble the seed sub-network. See network_query_api for the contract.

    Layer is fixed to first-shell: every edge with at least one endpoint in the
    seed set. Protein-level filters (degree / avg-score / reviewed / tissue)
    constrain only the *expanded* (non-seed) endpoints — a seed protein is the
    user's own input and is never filtered out, so a seed–seed edge always
    passes them.
    """
    # -- 1. Resolve seed proteins -----------------------------------------
    seed_ids, unresolved = _protein_ids_from_raw(raw_proteins)
    if len(seed_ids) > MAX_SEED_PROTEINS:
        seed_ids = seed_ids[:MAX_SEED_PROTEINS]

    protein_pks = list(seed_ids)
    if f.isoform_mode in ("isoforms", "both"):
        for pk in list(seed_ids):
            protein_pks.extend(iso.pk for iso in _get_isoforms(pk))
        protein_pks = list(dict.fromkeys(protein_pks))

    if not protein_pks:
        return {
            "node_count": 0,
            "edge_count": 0,
            "interactions": [],
            "seed_ids": [],
            "unresolved": unresolved,
            "truncated": False,
            "total_edges": 0,
            "error": "None of the identifiers could be resolved: "
            + ", ".join(unresolved),
        }

    query_set_pks = set(protein_pks)  # seeds + expanded isoforms
    tissue_pks = _tissue_pk_set(f)

    def _passes_protein_level(p1: Protein, p2: Protein) -> bool:
        if p1.pk not in query_set_pks and not _protein_passes(p1, f, tissue_pks):
            return False
        if p2.pk not in query_set_pks and not _protein_passes(p2, f, tissue_pks):
            return False
        return True

    results: list[dict] = []

    # -- 2. Interactions --------------------------------------------------
    if f.show in ("interactions", "both"):
        qs = _interaction_edge_qs(protein_pks, f)
        for ix in qs:
            p1, p2 = ix.protein_1, ix.protein_2
            if not _passes_protein_level(p1, p2):
                continue
            results.append(
                {
                    "id": ix.pk,
                    "a": _protein_display(p1),
                    "b": _protein_display(p2),
                    "score": round(ix.score, 4),
                    "source_count": ix.sources.all().count(),
                    "experiment_count": ix.experiments.all().count(),
                    "is_noninteraction": False,
                    "seed_interaction": p1.pk in query_set_pks
                    and p2.pk in query_set_pks,
                    "detail_url": reverse(
                        "hippie_website:interaction_detail", args=[ix.pk]
                    ),
                }
            )

    # -- 3. Non-interactions ---------------------------------------------
    # NonInteractions carry no sources / experiments / interaction types, so a
    # source-like filter excludes them entirely (mirrors protein_query_api).
    if f.show in ("noninteractions", "both") and not f.has_source_like:
        nqs = _noninteraction_edge_qs(protein_pks, f)
        for ni in nqs:
            p1, p2 = ni.protein_1, ni.protein_2
            if not _passes_protein_level(p1, p2):
                continue
            results.append(
                {
                    "id": ni.pk,
                    "a": _protein_display(p1),
                    "b": _protein_display(p2),
                    "score": round(ni.score, 4),
                    "source_count": None,
                    "experiment_count": None,
                    "is_noninteraction": True,
                    "seed_interaction": p1.pk in query_set_pks
                    and p2.pk in query_set_pks,
                    "detail_url": reverse(
                        "hippie_website:noninteraction_detail", args=[ni.pk]
                    ),
                }
            )

    # -- 4. Merge / sort / cap -------------------------------------------
    results.sort(key=lambda r: r["score"], reverse=True)
    total_edges = len(results)
    truncated = total_edges > MAX_NETWORK_EDGES
    if truncated:
        results = results[:MAX_NETWORK_EDGES]

    node_ids: set[int] = set()
    for r in results:
        node_ids.add(r["a"]["id"])
        node_ids.add(r["b"]["id"])

    return {
        "node_count": len(node_ids),
        "edge_count": len(results),
        "interactions": results,
        "seed_ids": sorted(query_set_pks),
        "unresolved": unresolved,
        "truncated": truncated,
        "total_edges": total_edges,
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
    match on gene symbol, UniProt accession, UniProt entry name, or a
    protein/gene synonym; exact match on the Entrez gene ID when the query is
    all digits.

    Synonyms are matched with ``Exists`` subqueries rather than reverse-FK
    joins so browse pagination/count stay correct (a join would multiply rows),
    mirroring the source-filter idiom in ``_filtered_protein_qs``.
    """
    prot_syn = ProteinSynonym.objects.filter(
        protein_id=OuterRef("pk"), synonym__icontains=q
    )
    gene_syn = GeneSynonym.objects.filter(
        gene_id=OuterRef("gene_id"), synonym__icontains=q
    )
    cond = (
        Q(gene__entrez_name__icontains=q)
        | Q(uniprot_accession__icontains=q)
        | Q(uniprot_name__icontains=q)
        | Exists(prot_syn)
        | Exists(gene_syn)
    )
    if q.isdigit():
        cond |= Q(gene__entrez_id=int(q))
    return cond


def _multi_protein_search_q(tokens: list[str]) -> Q:
    """
    OR together :func:`_protein_search_q` for each identifier so the browse
    pages return the union of substring matches. A single token behaves
    exactly like the original single-term search.
    """
    combined = Q()
    for tok in tokens:
        combined |= _protein_search_q(tok)
    return combined


def _filtered_protein_qs(request):
    """
    Build the ordered Protein queryset for the browse "Proteins" mode from the
    request's filter params. Shared by ``browse_proteins_api`` and ``browse_export_api``.

    ``degree`` / ``avg_score`` are read from denormalised columns (refreshed by
    ``recompute_protein_stats``), so degree/score filters and sorting are plain
    indexed clauses. The source filter uses two ``EXISTS`` subqueries over the
    M2M through table — one per protein side — so each rides the
    ``(protein_1, score)`` / ``(protein_2, score)`` indexes instead of an
    OR-correlated subquery across a dual interaction join.

    Reads the unified :class:`CommonFilters` contract shared with Protein Query
    and Interaction Query (``min_avg_score`` for the protein average-score gate,
    ``reviewed`` for the review-status toggle). Interaction-only filters on the
    contract (score/source-set/experiment/type, ``show``) are ignored here.
    """
    f = _common_filters_from_get(request.GET)
    q = request.GET.get("q", "").strip()

    sort_field = _BROWSE_SORT_FIELDS.get(
        request.GET.get("sort", "symbol"), "gene__entrez_name"
    )
    descending = request.GET.get("dir", "asc") == "desc"
    order = ("-" + sort_field) if descending else sort_field

    base_qs = Protein.objects.select_related("gene")
    if f.isoform_mode == "general":
        base_qs = base_qs.filter(isoform__isnull=True)
    elif f.isoform_mode == "isoforms":
        base_qs = base_qs.filter(isoform__isnull=False)

    if f.tissue_ids:
        base_qs = base_qs.expressed_in(f.tissue_ids, min_rpkm=f.min_rpkm)

    if f.source_ids:
        through = Interaction.sources.through.objects.filter(source_id__in=f.source_ids)
        p1 = through.filter(interaction__protein_1_id=OuterRef("pk"))
        p2 = through.filter(interaction__protein_2_id=OuterRef("pk"))
        base_qs = base_qs.filter(Exists(p1) | Exists(p2))

    tokens = _split_identifiers(q)
    if tokens:
        base_qs = base_qs.filter(_multi_protein_search_q(tokens))

    if f.min_degree is not None and f.min_degree > 0:
        base_qs = base_qs.filter(degree__gte=f.min_degree)
    if f.min_avg_score is not None and f.min_avg_score > 0:
        base_qs = base_qs.filter(avg_score__gte=f.min_avg_score)
    if f.reviewed == "reviewed":
        base_qs = base_qs.filter(is_reviewed=True)
    elif f.reviewed == "unreviewed":
        base_qs = base_qs.filter(is_reviewed=False)

    return base_qs.order_by(order, "pk")


# Browse interaction-table sort keys → union output-column aliases. Every
# column is server-side sortable; count sorts ride the denormalised
# ``n_sources`` / ``n_experiments`` indexes, the rest sort on joined columns.
_INT_SORT_FIELDS = {
    "symbol_a": "p1_symbol",
    "uniprot_a": "p1_acc",
    "entrez_a": "p1_entrez",
    "symbol_b": "p2_symbol",
    "uniprot_b": "p2_acc",
    "entrez_b": "p2_entrez",
    "score": "score",
    "sources": "n_sources",
    "experiments": "n_experiments",
}

# Column set (identical order) selected by both union legs so the two querysets
# are UNION-compatible.
_UNION_COLS = (
    "kind",
    "id",
    "score",
    "p1_symbol",
    "p1_acc",
    "p1_entrez",
    "p1_reviewed",
    "p2_symbol",
    "p2_acc",
    "p2_entrez",
    "p2_reviewed",
    "n_sources",
    "n_experiments",
)


def _symbol_expr(side: str):
    """Gene symbol for one interactor: gene.entrez_name, else uniprot_name."""
    return Coalesce(
        NullIf(F(f"{side}__gene__entrez_name"), Value("")),
        F(f"{side}__uniprot_name"),
    )


def _interaction_values_qs(qs):
    """Normalised ``.values()`` rows for the Interaction union leg (real counts)."""
    return qs.annotate(
        kind=Value("i", output_field=CharField()),
        p1_symbol=_symbol_expr("protein_1"),
        p1_acc=F("protein_1__uniprot_accession"),
        p1_entrez=F("protein_1__gene__entrez_id"),
        p1_reviewed=F("protein_1__is_reviewed"),
        p2_symbol=_symbol_expr("protein_2"),
        p2_acc=F("protein_2__uniprot_accession"),
        p2_entrez=F("protein_2__gene__entrez_id"),
        p2_reviewed=F("protein_2__is_reviewed"),
    ).values(*_UNION_COLS)


def _noninteraction_values_qs(qs):
    """Normalised ``.values()`` rows for the NonInteraction union leg.

    Non-interactions carry no source/experiment evidence, so both counts are a
    constant 0 — keeping the column set identical to the interaction leg so the
    two can be ``UNION``-ed and ordered together.
    """
    return qs.annotate(
        kind=Value("n", output_field=CharField()),
        p1_symbol=_symbol_expr("protein_1"),
        p1_acc=F("protein_1__uniprot_accession"),
        p1_entrez=F("protein_1__gene__entrez_id"),
        p1_reviewed=F("protein_1__is_reviewed"),
        p2_symbol=_symbol_expr("protein_2"),
        p2_acc=F("protein_2__uniprot_accession"),
        p2_entrez=F("protein_2__gene__entrez_id"),
        p2_reviewed=F("protein_2__is_reviewed"),
        n_sources=Value(0, output_field=IntegerField()),
        n_experiments=Value(0, output_field=IntegerField()),
    ).values(*_UNION_COLS)


def _browse_interaction_flags(f: "CommonFilters"):
    """Which union legs participate given the result-type toggle. Source-like
    filters (source/experiment/type) can never match a non-interaction, so the
    non-interaction leg drops out whenever one is active."""
    include_int = f.show in ("interactions", "both")
    include_nonint = f.show in ("noninteractions", "both") and not f.has_source_like
    return include_int, include_nonint


def _browse_interaction_base(request, f: "CommonFilters", q: str):
    """
    Build the (unordered) Interaction and NonInteraction querysets for the
    browse "Interactions" tab from the unified filter contract.

    Performance notes:
      * Isoform exclusion on the interaction leg reads the denormalised
        ``involves_isoform`` boolean — one indexed column instead of two
        ``protein_*__isoform`` anti-joins over 1.15M rows.
      * Interaction-level filters (score/source/experiment/type) reuse
        ``_apply_interaction_level_filters`` (EXISTS over indexed through tables).
      * Free-text search resolves matching protein PKs in a lean subquery, then
        filters either partner side via ``IN``.
    """
    int_qs = Interaction.objects.all()
    if f.isoform_mode == "general":
        int_qs = int_qs.filter(involves_isoform=False)
    elif f.isoform_mode == "isoforms":
        int_qs = int_qs.filter(involves_isoform=True)
    int_qs = _apply_interaction_level_filters(int_qs, f)

    # Non-interactions carry no evidence M2Ms, so only score / isoform / search
    # apply. Isoform gating uses the anti-join (no denormalised flag on this
    # far smaller table).
    nonint_qs = NonInteraction.objects.all()
    if f.isoform_mode == "general":
        nonint_qs = nonint_qs.filter(
            protein_1__isoform__isnull=True, protein_2__isoform__isnull=True
        )
    elif f.isoform_mode == "isoforms":
        nonint_qs = nonint_qs.filter(isoform_only_q())
    if f.min_score is not None:
        nonint_qs = nonint_qs.filter(score__gte=f.min_score)
    if f.max_score is not None:
        nonint_qs = nonint_qs.filter(score__lte=f.max_score)

    tokens = _split_identifiers(q)
    if tokens:
        pid_sub = Subquery(
            Protein.objects.filter(_multi_protein_search_q(tokens)).values("pk")
        )
        side_match = Q(protein_1__in=pid_sub) | Q(protein_2__in=pid_sub)
        int_qs = int_qs.filter(side_match)
        nonint_qs = nonint_qs.filter(side_match)

    return int_qs, nonint_qs


def _browse_interaction_rows(
    int_qs,
    nonint_qs,
    include_int,
    include_nonint,
    sort_key,
    descending,
    offset,
    limit,
):
    """Ordered, paginated, normalised rows across the interaction /
    non-interaction union — shared by ``browse_interactions_api`` (page) and
    ``browse_export_api`` (capped full set)."""
    legs = []
    if include_int:
        legs.append(_interaction_values_qs(int_qs))
    if include_nonint:
        legs.append(_noninteraction_values_qs(nonint_qs))
    if not legs:
        return []

    union = legs[0] if len(legs) == 1 else legs[0].union(legs[1], all=True)
    order_col = _INT_SORT_FIELDS.get(sort_key, "score")
    order = ("-" + order_col) if descending else order_col
    # ``Interaction`` and ``NonInteraction`` have independent, overlapping id
    # sequences, so ``id`` alone cannot disambiguate rows across the union —
    # ``kind`` must break the tie first to make pagination deterministic.
    page = union.order_by(order, "kind", "id")[offset : offset + limit]

    rows = []
    for r in page:
        is_ni = r["kind"] == "n"
        rows.append(
            {
                "id": r["id"],
                "protein_a": {
                    "symbol": r["p1_symbol"],
                    "uniprot_id": r["p1_acc"],
                    "entrez_id": r["p1_entrez"],
                    "is_reviewed": r["p1_reviewed"],
                },
                "protein_b": {
                    "symbol": r["p2_symbol"],
                    "uniprot_id": r["p2_acc"],
                    "entrez_id": r["p2_entrez"],
                    "is_reviewed": r["p2_reviewed"],
                },
                "score": round(r["score"], 4),
                "source_count": r["n_sources"],
                "experiment_count": r["n_experiments"],
                "is_noninteraction": is_ni,
                "detail_url": reverse(
                    "hippie_website:noninteraction_detail"
                    if is_ni
                    else "hippie_website:interaction_detail",
                    args=[r["id"]],
                ),
            }
        )
    return rows


def _too_many_identifiers_response(request):
    """
    Return a 400 ``JsonResponse`` when the ``q`` search string holds more than
    ``MAX_QUERY_PROTEINS`` identifiers, else ``None``. Shared by the browse
    list and export endpoints so all reject over-long input consistently.
    """
    count = len(_split_identifiers(request.GET.get("q", "")))
    if count > MAX_QUERY_PROTEINS:
        return JsonResponse(
            {
                "error": (
                    f"Too many proteins: {count} "
                    f"(max {MAX_QUERY_PROTEINS} per request)."
                )
            },
            status=400,
        )
    return None


@require_GET
def browse_proteins_api(request):
    """
    GET /api/browse/proteins/?offset=<int>&limit=<int>&q=<text>
                    &sort=<key>&dir=<asc|desc>
                    &tissue=<id>&tissue=<id>&source=<id>&source=<id>
                    &min_degree=<int>&min_score=<float>&min_rpkm=<float>

    Returns a single page of proteins (server-side pagination):
    {"total": <int>, "proteins": [ {id, symbol, uniprot_id, entrez_id,
                                     degree, avg_score, is_reviewed}, ... ]}
    """
    too_many = _too_many_identifiers_response(request)
    if too_many is not None:
        return too_many

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
            "is_reviewed": p.is_reviewed,
        }
        for p in base_qs[offset : offset + limit]
    ]

    return JsonResponse({"total": total, "proteins": proteins})


@require_GET
def browse_interactions_api(request):
    """
    GET /api/browse/interactions/?offset&limit&q&show&min_score&max_score
        &source&experiment&interaction_type&isoform_mode&sort&dir

    Server-side paginated interaction table for the browse "Interactions" tab.
    Honours the unified filter contract including the interactions /
    non-interactions / both result-type toggle (``show``); the "both" view is a
    server-side ``UNION`` of the two tables, ordered and paginated together. All
    columns are sortable (``sort`` = symbol_a/uniprot_a/entrez_a/…/score/
    sources/experiments). Non-interaction rows carry ``is_noninteraction: true``
    and zero evidence counts.

    Returns:
    {"total": <int>, "interactions": [ {id, protein_a, protein_b, score,
        source_count, experiment_count, is_noninteraction, detail_url}, ... ]}
    """
    too_many = _too_many_identifiers_response(request)
    if too_many is not None:
        return too_many

    try:
        offset = max(0, int(request.GET.get("offset", 0)))
        limit = min(200, max(1, int(request.GET.get("limit", 50))))
    except (TypeError, ValueError):
        offset, limit = 0, 50

    f = _common_filters_from_get(request.GET)
    q = request.GET.get("q", "").strip()
    sort_key = request.GET.get("sort", "score")
    descending = request.GET.get("dir", "desc") != "asc"

    int_qs, nonint_qs = _browse_interaction_base(request, f, q)
    include_int, include_nonint = _browse_interaction_flags(f)

    total = 0
    if include_int:
        total += _cached_total(_count_cache_key("browse_int", request), int_qs)
    if include_nonint:
        total += _cached_total(_count_cache_key("browse_nonint", request), nonint_qs)

    interactions = _browse_interaction_rows(
        int_qs,
        nonint_qs,
        include_int,
        include_nonint,
        sort_key,
        descending,
        offset,
        limit,
    )

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
    too_many = _too_many_identifiers_response(request)
    if too_many is not None:
        return too_many

    mode = request.GET.get("mode", "proteins")

    if mode == "interactions":
        f = _common_filters_from_get(request.GET)
        q = request.GET.get("q", "").strip()
        sort_key = request.GET.get("sort", "score")
        descending = request.GET.get("dir", "desc") != "asc"
        int_qs, nonint_qs = _browse_interaction_base(request, f, q)
        include_int, include_nonint = _browse_interaction_flags(f)
        total = 0
        if include_int:
            total += int_qs.count()
        if include_nonint:
            total += nonint_qs.count()
        header = [
            "Gene A",
            "UniProt A",
            "Entrez A",
            "Gene B",
            "UniProt B",
            "Entrez B",
            "Score",
            "Sources",
            "Experiments",
            "Type",
            "Review Type A",
            "Review Type B",
        ]

        def rows():
            yield _tsv_line(header)
            for r in _browse_interaction_rows(
                int_qs,
                nonint_qs,
                include_int,
                include_nonint,
                sort_key,
                descending,
                0,
                EXPORT_CAP,
            ):
                a, b = r["protein_a"], r["protein_b"]
                yield _tsv_line(
                    [
                        a["symbol"],
                        a["uniprot_id"],
                        a["entrez_id"],
                        b["symbol"],
                        b["uniprot_id"],
                        b["entrez_id"],
                        r["score"],
                        r["source_count"],
                        r["experiment_count"],
                        "Non-Interaction" if r["is_noninteraction"] else "Interaction",
                        "reviewed" if a["is_reviewed"] else "unreviewed",
                        "reviewed" if b["is_reviewed"] else "unreviewed",
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
            "Review Type",
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
                        "reviewed" if p.is_reviewed else "unreviewed",
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


def _filter_option_lists() -> dict:
    """Tissue / source / experiment / interaction-type option lists for the
    filter controls. Shared by ``browse_filter_meta`` (the query pages) and
    ``_ml_filter_meta`` (the ML-splits page). Sources are limited to those with
    at least one connected interaction."""
    from .models import Source, ExperimentType

    return {
        "tissues": list(Tissue.objects.order_by("name").values("id", "name")),
        "sources": list(
            Source.objects.filter(n_connected_interactions__gt=0)
            .order_by("name")
            .values("id", "name")
        ),
        "experiments": list(
            ExperimentType.objects.order_by("name").values("id", "name")
        ),
        "interaction_types": list(
            InteractionType.objects.order_by("name").values("id", "name")
        ),
    }


@require_GET
def browse_filter_meta(request):
    """
    GET /api/browse/filters/

    Returns the data needed to populate the filter controls:
    {
        "tissues":     [{ "id": <int>, "name": "<str>" }, ...],
        "sources":     [{ "id": <int>, "name": "<str>" }, ...],
        "experiments": [{ "id": <int>, "name": "<str>" }, ...],
        "interaction_types": [{ "id": <int>, "name": "<str>" }, ...]
    }
    """
    return JsonResponse(_filter_option_lists())


# ---------------------------------------------------------------------------
# Interaction detail view
# ---------------------------------------------------------------------------


def _protein_detail_ctx(protein) -> dict:
    """Interactor context dict for the detail templates (protein_pair_base.html):
    the raw protein plus its display accession, Entrez id, and gene symbol."""
    return {
        "protein": protein,
        "uniprot_id": protein.uniprot_accession,
        "gene_id": protein.gene.entrez_id or None,
        "symbol": protein.gene.entrez_name or protein.uniprot_name,
    }


def _digger_ctx(p1: Protein, p2: Protein) -> dict:
    """DIGGER cross-links for the "Further information" card, shared by the
    interaction and non-interaction detail pages.

    One extra query resolves which endpoints are isoforms (and loads their
    ENST/ENSP); canonical proteins fall back to the already-``select_related``
    ``gene``. See ``digger_links.py`` for the URL rules.
    """
    from hippie_website.digger_links import interaction_digger, protein_digger_url

    isos = {
        i.pk: i
        for i in Isoform.objects.select_related("gene").filter(pk__in=[p1.pk, p2.pk])
    }

    def _one(p: Protein) -> dict:
        iso = isos.get(p.pk)
        if iso is not None:
            return {
                "is_isoform": True,
                "url": protein_digger_url(
                    is_isoform=True,
                    ensg=iso.gene.ensg,
                    enst=iso.enst,
                    ensp=iso.ensp,
                ),
            }
        return {
            "is_isoform": False,
            "url": protein_digger_url(
                is_isoform=False, ensg=p.gene.ensg, enst=[], ensp=[]
            ),
        }

    def _transcript_with_fallback(i: Isoform) -> str:
        """Return the ENST if present, else the ENSP, else empty string. Used for DIGGER links."""
        if i.enst:
            return i.enst
        if i.ensp:
            return i.ensp
        return ""

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
        ),
    }


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
    from hippie_website.source_links import pair_search_url

    sources = list(interaction.sources.all())
    for source in sources:
        source.pair_url = pair_search_url(
            source.name, p1.uniprot_accession, p2.uniprot_accession
        )

    context = {
        "interaction": interaction,
        "p1": _protein_detail_ctx(p1),
        "p2": _protein_detail_ctx(p2),
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
        "digger": _digger_ctx(p1, p2),
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
    context = {
        "noninteraction": noninteraction,
        "p1": _protein_detail_ctx(p1),
        "p2": _protein_detail_ctx(p2),
        "bait_prey_total_tested": bait_prey_total_tested,
        "bait_prey_times_observed": bait_prey_times_observed,
        # Shared with protein_pair_base.html
        "pair_score": noninteraction.score,
        "pair_label": "Non-Interaction Evidence",
        "is_noninteraction": True,
        "digger": _digger_ctx(p1, p2),
    }
    return render(request, "hippie_website/noninteraction_detail.html", context)


# ---------------------------------------------------------------------------
# Static pages
# ---------------------------------------------------------------------------


@require_GET
def download_view(request):
    return render(request, "hippie_website/download.html", {})


# Directory holding the downloadable HIPPIE release files (server-only).
HIPPIE_VERSIONS_DIR = settings.BASE_DIR / "data" / "hippie_versions"
TECH_SCORING_VIEW_DIR = settings.BASE_DIR / "data" / "technique_scores"


# This is just a backup function in case the application is not run with docker.
# In production the download files are served with apache
@require_GET
def download_dataset(request, filename: str):
    """Serve a downloadable file as an attachment.

    Searches ``HIPPIE_VERSIONS_DIR`` then ``TECH_SCORING_VIEW_DIR``.
    ``filename`` is collapsed to a single path component to guard against
    directory traversal (``..``) and absolute paths.
    """
    safe_name = os.path.basename(filename)
    if safe_name != filename:
        raise Http404("File not found")
    for directory in (HIPPIE_VERSIONS_DIR, TECH_SCORING_VIEW_DIR):
        file_path = (directory / safe_name).resolve()
        if file_path.parent == directory.resolve() and file_path.is_file():
            return FileResponse(
                open(file_path, "rb"), as_attachment=True, filename=safe_name
            )
    raise Http404("File not found")


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
            "isoform_mode": parse_isoform_mode(body.get("isoform_mode")),
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
    return _filter_option_lists()


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
        "isoform_mode": parse_isoform_mode(request.GET.get("isoform_mode")),
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
    body, err = _parse_json_body(request)
    if err:
        return err
    params = _validate_split_params(body)  # raises 400 on bad input
    job = SplitJob.objects.create(params=params)
    run_split_job.delay(str(job.id))
    return JsonResponse({"job_id": str(job.id), "status": job.status}, status=202)


def _protein_filtered_qs(params):
    """Protein queryset after the protein-level filters (for the stats box)."""
    pqs = Protein.objects.all()
    if params.isoform_mode == "general":
        pqs = pqs.filter(isoform__isnull=True)
    elif params.isoform_mode == "isoforms":
        pqs = pqs.filter(isoform__isnull=False)
    return apply_protein_level_filters(
        pqs,
        tissue_ids=params.tissue_ids,
        min_rpkm=params.min_rpkm,
        min_degree=params.min_degree,
        min_avg_score=params.min_avg_score,
    )


def _protein_stats(
    params, degree_by_node: dict[int, int], score_sum_by_node: dict[int, float], iqs
) -> dict:
    """Protein-side stats for the filter-preview box.

    ``degree_by_node`` / ``score_sum_by_node`` come from ``_interaction_stats``
    over ``iqs``: per-protein surviving-edge count and score sum under the full
    interaction filter. Because ``build_interaction_queryset`` gates every edge
    on BOTH endpoints passing the protein-level filters, ``degree_by_node.keys()``
    is a subset of the protein-filtered queryset — so every surviving protein
    already passes the protein filter, and the medians can be read straight
    from these dicts with no second protein-table scan.

    ``median_degree`` and ``median_avg_score`` are therefore filter-aware (they
    reflect only surviving edges), not the denormalized global ``Protein.degree``
    / ``Protein.avg_score`` columns. A protein that passes the protein-level
    filter but has zero surviving edges is an orphan: excluded from the medians
    and counted in ``n_orphaned_by_filter``."""
    import statistics

    from .models import Isoform

    pqs = _protein_filtered_qs(params)

    # Surviving proteins (>=1 edge under the full filter) drive the medians.
    degrees = list(degree_by_node.values())
    # Per-protein average over its SURVIVING edges (filter-aware, mirrors
    # median_degree); degree >= 1 for every key, so no divide-by-zero.
    avg_scores = [score_sum_by_node[pk] / deg for pk, deg in degree_by_node.items()]
    n_surviving = len(degrees)

    # Counts that genuinely need the protein table: two indexed aggregates
    # instead of iterating every protein row. .distinct() on pk collapses
    # tissue-join duplicates when a tissue filter is active.
    n_pass_protein_filter = pqs.values("pk").distinct().count()

    # Isoforms: intersect surviving pks with isoform pks in Python (no giant
    # ``pk IN (...20k ids...)`` clause).
    n_isoforms = (
        len(degree_by_node.keys() & set(Isoform.objects.values_list("pk", flat=True)))
        if params.isoform_mode != "general"
        else 0
    )

    # Proteins removed by the protein-level filter, relative to the full protein
    # universe (respecting the isoform mode so the base matches the start of
    # ``_protein_filtered_qs``). Distinct from ``n_orphaned_by_filter``, which
    # counts proteins that pass this filter but lose all edges.
    if params.isoform_mode == "general":
        base_universe = Protein.objects.filter(isoform__isnull=True).count()
    elif params.isoform_mode == "isoforms":
        base_universe = Protein.objects.filter(isoform__isnull=False).count()
    else:
        base_universe = Protein.objects.count()
    n_filtered_out = base_universe - n_pass_protein_filter

    return {
        "n_proteins": n_surviving,
        "median_degree": statistics.median(degrees) if degrees else 0,
        "median_avg_score": (
            round(statistics.median(avg_scores), 4) if avg_scores else None
        ),
        "n_filtered_out": n_filtered_out,
        "n_orphaned_by_filter": n_pass_protein_filter - n_surviving,
        "n_isoforms": n_isoforms,
        # Per-protein degree distribution (moved here from the interaction box:
        # degree is a protein-level attribute).
        "degree_histogram": _bucket_degrees(degree_by_node),
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


def _interaction_stats(iqs) -> tuple[dict, dict[int, int], dict[int, float]]:
    from django.db.models import Count, F
    from django.db.models.functions import Floor

    from .models import Interaction

    # Per-node degree + score-sum via DB-side GROUP BY (one per FK side), riding
    # the (protein_1, score) / (protein_2, score) covering indexes — so we never
    # stream the filtered edges into Python. Shared with recompute_protein_stats
    # via query_filters.group_by_side. A self-loop (protein_1 == protein_2) lands
    # in both side1 and side2, so it counts twice — matching the old
    # edge-by-edge loop that incremented both endpoints.
    side1 = group_by_side(iqs, "protein_1_id")
    side2 = group_by_side(iqs, "protein_2_id")
    degree_by_node: dict[int, int] = {}
    score_sum_by_node: dict[int, float] = {}
    for pk in side1.keys() | side2.keys():
        c1, s1 = side1.get(pk, (0, 0.0))
        c2, s2 = side2.get(pk, (0, 0.0))
        degree_by_node[pk] = c1 + c2
        score_sum_by_node[pk] = s1 + s2

    # Score distribution from one GROUP BY over 100 fine bins (bin =
    # floor(score * 100); score == 1.0 clamps into the last bin). n, the 10-bin
    # display histogram, and the median all derive from it — no extra scan.
    score_fine = [0] * 100
    n = 0
    for r in (
        iqs.annotate(_bin=Floor(F("score") * 100.0))
        .values("_bin")
        .annotate(c=Count("id"))
    ):
        score_fine[min(int(r["_bin"]), 99)] += r["c"]
        n += r["c"]
    score_hist = [sum(score_fine[i * 10 : i * 10 + 10]) for i in range(10)]

    # Median score from the fine histogram (O(1) memory).
    median_score = None
    if n:
        half, cum = n / 2, 0
        for i, c in enumerate(score_fine):
            cum += c
            if cum >= half:
                median_score = round((i + 0.5) / 100, 4)
                break

    n_experiments = (
        Interaction.experiments.through.objects.filter(interaction__in=iqs)
        .values("experimenttype_id")
        .distinct()
        .count()
    )

    return (
        {
            "n_interactions": n,
            # Label = lower bin edge (e.g. "0.3" = bucket [0.3, 0.4)); short enough
            # to read along a 10-bin X axis.
            "score_histogram": [
                {"label": f"{i / 10:.1f}", "count": c} for i, c in enumerate(score_hist)
            ],
            "median_score": median_score,
            "n_experiments": n_experiments,
        },
        degree_by_node,
        score_sum_by_node,
    )


@require_POST
def browse_splits_stats(request):
    """POST the split filter payload, get on-demand protein + interaction
    statistics for the filter-preview boxes. Same param shape as
    ``browse_splits_create``."""
    from .services.generate_splits import SplitParams, build_interaction_queryset

    body, err = _parse_json_body(request)
    if err:
        return err
    params = SplitParams(**_validate_split_params(body))
    iqs = build_interaction_queryset(params)
    interaction_stats, degree_by_node, score_sum_by_node = _interaction_stats(iqs)
    return JsonResponse(
        {
            "protein": _protein_stats(params, degree_by_node, score_sum_by_node, iqs),
            "interaction": interaction_stats,
        }
    )


@require_GET
def browse_splits_status(request, job_id):
    job = get_object_or_404(SplitJob, pk=job_id)
    # Queue position = number of jobs still PENDING that were created before this
    # one (FIFO by created_at). 0 once the job is picked up (RUNNING/DONE/FAILED)
    # or when nothing precedes it. Lets each run card show its wait in line.
    # `id` (tie-broken on) is a random UUID, not a real ordinal, but it makes
    # the count deterministic when two jobs share a created_at tie instead of
    # both reporting the same position.
    queue_position = (
        SplitJob.objects.filter(status="PENDING")
        .filter(
            Q(created_at__lt=job.created_at)
            | Q(created_at=job.created_at, id__lt=job.id)
        )
        .count()
        if job.status == "PENDING"
        else 0
    )
    return JsonResponse(
        {
            "status": job.status,
            "step": job.step,
            "progress": job.progress,
            "params": job.params,
            "created_at": job.created_at.isoformat(),
            "queue_position": queue_position,
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
