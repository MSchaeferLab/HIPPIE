"""Protein-query services: identifier resolution and interaction-row assembly.

Extracted from ``views.py`` so the website and the MCP server run the same query
semantics instead of two implementations that can drift. The views keep request
parsing and response construction; everything here takes plain Python arguments
and returns plain Python data.

The filter contract lives here too (:class:`CommonFilters`). Views build it from
query params via ``views._common_filters_from_get`` / ``_from_body``; other
callers use :meth:`CommonFilters.from_dict`.
"""

import re
from dataclasses import dataclass, field

from django.db.models import Q, QuerySet
from django.urls import reverse

from ..models import Interaction, Isoform, NonInteraction, Protein
from ..query_filters import (
    apply_interaction_level_filters,
    canonical_or_queried_q,
    isoform_only_q,
    parse_isoform_mode,
)

# Max distinct identifiers accepted by the single-protein search endpoints
# (Protein Query, Browse). Mirrors BATCH_LIMIT=200 on the interaction endpoint.
MAX_QUERY_PROTEINS = 50

_IDENT_SPLIT = re.compile(r"[\s,;]+")

_SHOW_MODES = ("interactions", "noninteractions", "both")
_REVIEWED_MODES = ("both", "reviewed", "unreviewed")


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------


def safe_int(value: object) -> int | None:
    """Coerce to int, returning None for anything unparseable."""
    if value in (None, ""):
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def safe_float(value: object) -> float | None:
    """Coerce to float, returning None for anything unparseable."""
    if value in (None, ""):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def int_id_list(values: object) -> list[int]:
    """Keep only the digit-like entries of an iterable, as ints."""
    if not values:
        return []
    return [int(v) for v in values if str(v).isdigit()]  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Shared query filters (Protein Query + Interaction Query + MCP)
#
# One filter contract shared by the two React query pages so the single
# FilterBox component emits identical params everywhere, and by the MCP tools so
# an agent-issued query means exactly what the same query means on the website.
# Interaction-level filters (score / source / experiment / interaction-type) are
# applied to an Interaction queryset (or matched against a prefetched
# Interaction); protein-level filters (degree / avg-score / reviewed / tissue)
# are checked per Protein in Python — query result sets are small (one protein's
# partners, or a user-supplied pair list), so no full-table scan is involved.
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

    @classmethod
    def from_dict(cls, data: dict) -> "CommonFilters":
        """Build from a plain dict of already-resolved values.

        The non-request entry point: callers that have integer PK lists in hand
        (the MCP tools, after ``filter_lookup`` resolution) use this instead of
        the query-param builders in ``views``. Unknown ``show`` / ``reviewed``
        values fall back to their defaults rather than raising, matching the
        request-side behaviour.
        """
        show = data.get("show") or "interactions"
        if show not in _SHOW_MODES:
            show = "interactions"
        reviewed = data.get("reviewed") or "both"
        if reviewed not in _REVIEWED_MODES:
            reviewed = "both"
        return cls(
            show=show,
            isoform_mode=parse_isoform_mode(data.get("isoform_mode")),
            min_score=safe_float(data.get("min_score")),
            max_score=safe_float(data.get("max_score")),
            source_ids=int_id_list(data.get("source_ids")),
            experiment_ids=int_id_list(data.get("experiment_ids")),
            interaction_type_ids=int_id_list(data.get("interaction_type_ids")),
            tissue_ids=int_id_list(data.get("tissue_ids")),
            min_rpkm=safe_float(data.get("min_rpkm")),
            min_degree=safe_int(data.get("min_degree")),
            min_avg_score=safe_float(data.get("min_avg_score")),
            reviewed=reviewed,
        )


# ---------------------------------------------------------------------------
# Identifier handling
# ---------------------------------------------------------------------------


def split_identifiers(raw: str) -> list[str]:
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


def protein_display(protein: Protein, isoform_uid: str | None = None) -> dict:
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


def protein_ids_from_raw(raw: str) -> tuple[list[int], list[str]]:
    """
    Resolve a delimited string of identifiers (comma/whitespace/semicolon) to
    Protein PKs. Returns (resolved_pks, unresolved_identifiers).
    """
    protein_ids: list[int] = []
    unresolved: list[str] = []
    seen: set[int] = set()
    for ident in split_identifiers(raw):
        pk = Protein.objects.resolve(ident)
        if pk is not None:
            pk = pk.values_list("pk", flat=True).first()

        if pk is not None and pk not in seen:
            protein_ids.append(pk)
            seen.add(pk)
        elif pk is None:
            unresolved.append(ident)
    return protein_ids, unresolved


def get_isoforms(protein_pk: int) -> list[Isoform]:
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


@dataclass
class ResolvedProteins:
    """Outcome of resolving a list of raw identifiers.

    ``protein_pks`` is the union of the directly resolved proteins and, when the
    isoform mode expands, their isoforms — it is what the edge querysets take.
    ``isoform_uid_map`` carries the isoform-specific accession for display, keyed
    by PK; a canonical protein has no entry.
    """

    resolved: list[Protein] = field(default_factory=list)
    isoforms: list[Isoform] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    protein_pks: list[int] = field(default_factory=list)
    isoform_uid_map: dict[int, str] = field(default_factory=dict)

    @property
    def found_any(self) -> bool:
        return bool(self.resolved)


def resolve_proteins(tokens: list[str], isoform_mode: str) -> ResolvedProteins:
    """Resolve identifiers to Proteins, order-preserving and deduped.

    Each token goes through ``Protein.objects.resolve()`` (gene symbol, UniProt
    accession/entry name, Entrez id, isoform accession, or Ensembl id via
    synonyms). Tokens that match nothing land in ``unresolved`` rather than
    failing the whole call.

    When ``isoform_mode`` is "isoforms" or "both", every canonical protein also
    contributes its known isoforms. A queried identifier that is itself an
    isoform is never expanded further.
    """
    out = ResolvedProteins()
    resolved_pks: set[int] = set()

    for tok in tokens:
        protein = Protein.objects.resolve(tok).select_related("gene").first()
        if protein is None:
            out.unresolved.append(tok)
        elif protein.pk not in resolved_pks:
            resolved_pks.add(protein.pk)
            out.resolved.append(protein)

    # A queried identifier may itself be an isoform (resolve() annotates
    # ``isoform_uniprot_id``); when isoform_mode expands, each canonical seed
    # also contributes its known isoforms. All are unioned into protein_pks.
    out.protein_pks = [p.pk for p in out.resolved]
    for p in out.resolved:
        uid = getattr(p, "isoform_uniprot_id", None)
        if uid:
            out.isoform_uid_map[p.pk] = uid

    if isoform_mode in ("isoforms", "both"):
        seen_iso: set[int] = set(resolved_pks)
        for p in out.resolved:
            for iso in get_isoforms(p.pk):
                if iso.pk not in seen_iso:
                    seen_iso.add(iso.pk)
                    out.isoforms.append(iso)
                    out.protein_pks.append(iso.pk)
                    out.isoform_uid_map[iso.pk] = iso.uniprot_accession

    return out


# ---------------------------------------------------------------------------
# Filter application
# ---------------------------------------------------------------------------


def apply_common_interaction_filters(qs: QuerySet, f: CommonFilters) -> QuerySet:
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


def interaction_matches(interaction: Interaction, f: CommonFilters) -> bool:
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


def tissue_pk_set(f: CommonFilters) -> set[int] | None:
    """PKs of proteins expressed in any selected tissue (≥ min_rpkm), or None
    when no tissue filter is active. Computed once per request."""
    if not f.tissue_ids:
        return None
    return set(
        Protein.objects.expressed_in(f.tissue_ids, min_rpkm=f.min_rpkm).values_list(
            "pk", flat=True
        )
    )


def protein_passes(
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


# ---------------------------------------------------------------------------
# Edge querysets
# ---------------------------------------------------------------------------


def interaction_edge_qs(protein_pks: list[int], f: CommonFilters) -> QuerySet:
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
    return apply_common_interaction_filters(qs, f)


def noninteraction_edge_qs(protein_pks: list[int], f: CommonFilters) -> QuerySet:
    """Ordered NonInteraction queryset for the query / network pages: the
    isoform-mode gate (see interaction_edge_qs) plus the score range.
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


# ---------------------------------------------------------------------------
# Row assembly
# ---------------------------------------------------------------------------


def _orient(edge, protein_pks_set: set[int]) -> tuple[Protein, Protein]:
    """Split an edge into ``(query_side, partner)``.

    ``protein_1`` wins when both endpoints were queried, which is what makes the
    protein-level filters apply to exactly one side per edge.
    """
    if edge.protein_1_id in protein_pks_set:
        return edge.protein_1, edge.protein_2
    return edge.protein_2, edge.protein_1


def _interaction_row(
    interaction: Interaction,
    query_side: Protein,
    partner: Protein,
    uid_map: dict[int, str],
) -> dict:
    return {
        "id": interaction.pk,
        "query_side": protein_display(query_side, uid_map.get(query_side.pk)),
        "partner": protein_display(partner),
        "score": round(interaction.score, 4),
        # Reads the prefetch cache, so this is a len(), not a COUNT query.
        "source_count": interaction.sources.all().count(),
        "experiment_count": interaction.experiments.all().count(),
        "is_noninteraction": False,
        "detail_url": reverse(
            "hippie_website:interaction_detail", args=[interaction.pk]
        ),
    }


def _noninteraction_row(
    ni: NonInteraction,
    query_side: Protein,
    partner: Protein,
    uid_map: dict[int, str],
) -> dict:
    return {
        "id": ni.pk,
        "query_side": protein_display(query_side, uid_map.get(query_side.pk)),
        "partner": protein_display(partner),
        "score": round(ni.score, 4),
        # Non-interactions carry no source / experiment evidence.
        "source_count": None,
        "experiment_count": None,
        "is_noninteraction": True,
        "detail_url": reverse("hippie_website:noninteraction_detail", args=[ni.pk]),
    }


def interaction_rows(
    protein_pks: list[int],
    f: CommonFilters,
    isoform_uid_map: dict[int, str] | None = None,
) -> list[dict]:
    """Assemble the edge rows for a set of queried proteins.

    Walks the interaction and/or non-interaction legs selected by ``f.show``,
    orients each edge so ``query_side`` is the queried protein and ``partner``
    is the other end, and applies the protein-level filters to the partner side.
    In "both" mode the two legs are merged and re-sorted by score descending.

    An edge touching two queried proteins appears once, because the underlying
    queryset returns it once.

    Returns *every* match. A caller that only wants the top few should use
    :func:`interaction_rows_page`, which can push the cap into the query.
    """
    uid_map = isoform_uid_map or {}
    protein_pks_set = set(protein_pks)
    tissue_pks = tissue_pk_set(f)
    results: list[dict] = []

    if f.show in ("interactions", "both"):
        for interaction in interaction_edge_qs(protein_pks, f):
            query_side, partner = _orient(interaction, protein_pks_set)
            # Protein-level filters apply to the partner (B) side.
            if not protein_passes(partner, f, tissue_pks):
                continue
            results.append(_interaction_row(interaction, query_side, partner, uid_map))

    if f.show in ("noninteractions", "both") and not f.has_source_like:
        for ni in noninteraction_edge_qs(protein_pks, f):
            query_side, partner = _orient(ni, protein_pks_set)
            # Protein-level filters apply to the partner (B) side.
            if not protein_passes(partner, f, tissue_pks):
                continue
            results.append(_noninteraction_row(ni, query_side, partner, uid_map))

    # For "both" mode, re-sort by score descending (interactions first for ties)
    if f.show == "both":
        results.sort(key=lambda r: r["score"], reverse=True)

    return results


def interaction_rows_page(
    protein_pks: list[int],
    f: CommonFilters,
    isoform_uid_map: dict[int, str] | None = None,
    *,
    limit: int,
) -> tuple[list[dict], int]:
    """The top ``limit`` edge rows by score, plus the true total match count.

    For a caller that shows a capped result and states the total — the MCP tools
    — building every row first is pure waste: a hub protein has thousands of
    partners, and each discarded row costs a serialisation pass plus its share of
    the source / experiment prefetch.

    So when no protein-level filter is active, the cap goes into the query
    (``LIMIT``, riding the existing ``-score`` ordering) and the total comes from
    a separate lean ``COUNT`` — the interaction-level filters are ``EXISTS``
    subqueries that never multiply rows, so that count is exact. Nothing is
    annotated onto the counted queryset, per the ``browse_proteins_api`` lesson.

    When a protein-level filter *is* active the fallback is today's behaviour:
    those filters run in Python against the partner side, so the database cannot
    know how many rows survive them, and the honest total needs every row built.
    Correctness over speed — a wrong total is worse than a slow one.

    In "both" mode each leg is capped separately before the merge, which still
    yields the global top ``limit``: the top *k* of a union is contained in the
    union of the per-leg top *k*.
    """
    if f.has_protein_level:
        rows = interaction_rows(protein_pks, f, isoform_uid_map)
        return rows[:limit], len(rows)

    uid_map = isoform_uid_map or {}
    protein_pks_set = set(protein_pks)
    results: list[dict] = []
    total = 0

    if f.show in ("interactions", "both"):
        qs = interaction_edge_qs(protein_pks, f)
        total += qs.count()
        results.extend(
            _interaction_row(i, *_orient(i, protein_pks_set), uid_map)
            for i in qs[:limit]
        )

    if f.show in ("noninteractions", "both") and not f.has_source_like:
        qs = noninteraction_edge_qs(protein_pks, f)
        total += qs.count()
        results.extend(
            _noninteraction_row(n, *_orient(n, protein_pks_set), uid_map)
            for n in qs[:limit]
        )

    # Stable, so interactions still win ties against non-interactions.
    if f.show == "both":
        results.sort(key=lambda r: r["score"], reverse=True)

    return results[:limit], total
