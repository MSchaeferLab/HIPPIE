"""Pair-lookup services: "does A interact with B, and on what evidence".

Extracted from ``views.py``. Every input pair yields exactly one row (or, in
isoform-expansion mode, one row per isoform combination that has a recorded
interaction), and a score of ``-1.0`` means "not found" — either an identifier
was unknown, or no record exists between the two proteins, or the record failed
the active filters. Callers distinguish those cases by whether the row carries
protein metadata (``uniprot_a`` non-empty means both sides resolved).
"""

from django.db.models import Q
from django.urls import reverse

from ..models import Interaction, Isoform, NonInteraction, Protein
from .queries import (
    CommonFilters,
    apply_common_interaction_filters,
    get_isoforms,
    interaction_matches,
    protein_display,
    protein_passes,
    tissue_pk_set,
)

MAX_PAIRS = 5_000  # hard limit enforced server-side and client-side
BATCH_LIMIT = 200  # max pairs accepted per individual API call


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------


def pair_not_found(
    input_a: str,
    input_b: str,
    input_order: int,
    *,
    is_noninteraction: bool,
    ua: dict | None = None,
    ub: dict | None = None,
) -> dict:
    """Build the not-found (score -1) row for an input pair.

    When ``ua`` / ``ub`` (the ``protein_display`` dicts) are given the proteins
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


def pair_row(
    ua: dict,
    ub: dict,
    *,
    input_a: str,
    input_b: str,
    input_order: int,
    score: float,
    source_count: int | None,
    experiment_count: int | None,
    obj_pk: int,
    is_noninteraction: bool,
) -> dict:
    """Build a found-pair result row from two ``protein_display`` dicts."""
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


# ---------------------------------------------------------------------------
# Single-pair resolution
# ---------------------------------------------------------------------------


def resolve_pair(
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
        return pair_not_found(
            input_a, input_b, input_order, is_noninteraction=is_noninteraction
        )

    p1, p2 = (
        (protein_a, protein_b)
        if protein_a.pk <= protein_b.pk
        else (protein_b, protein_a)
    )
    ua = protein_display(protein_a)
    ub = protein_display(protein_b)

    def _nf() -> dict:
        return pair_not_found(
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
            or not protein_passes(protein_a, f, tissue_pks)
            or not protein_passes(protein_b, f, tissue_pks)
        ):
            return _nf()
        return pair_row(
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
        not interaction_matches(obj, f)
        or not protein_passes(protein_a, f, tissue_pks)
        or not protein_passes(protein_b, f, tissue_pks)
    ):
        return _nf()
    return pair_row(
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


def resolve_interaction_pair(
    input_a: str,
    input_b: str,
    input_order: int,
    f: CommonFilters | None = None,
    tissue_pks: set[int] | None = None,
) -> dict:
    """Resolve a pair against the Interaction table (see resolve_pair)."""
    return resolve_pair(
        input_a, input_b, input_order, f, tissue_pks, is_noninteraction=False
    )


def resolve_noninteraction_pair(
    input_a: str,
    input_b: str,
    input_order: int,
    f: CommonFilters | None = None,
    tissue_pks: set[int] | None = None,
) -> dict:
    """Resolve a pair against the NonInteraction table (see resolve_pair)."""
    return resolve_pair(
        input_a, input_b, input_order, f, tissue_pks, is_noninteraction=True
    )


def resolve_interaction_pair_with_isoforms(
    input_a: str,
    input_b: str,
    input_order: int,
    isoform_cache: dict,
    f: CommonFilters | None = None,
    tissue_pks: set[int] | None = None,
    isoform_mode: str = "both",
) -> list[dict]:
    """
    Like resolve_interaction_pair but expands each canonical protein side to
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
        return [resolve_interaction_pair(input_a, input_b, input_order, f, tissue_pks)]

    # Cached isoform lookup ---------------------------------------------------
    def cached_isoforms(pk: int) -> list:
        if pk not in isoform_cache:
            isoform_cache[pk] = get_isoforms(pk)
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
                pair_not_found(
                    input_a,
                    input_b,
                    input_order,
                    is_noninteraction=False,
                    ua=protein_display(protein_a),
                    ub=protein_display(protein_b),
                )
            ]
        return [resolve_interaction_pair(input_a, input_b, input_order, f, tissue_pks)]

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
        interactions_qs = apply_common_interaction_filters(interactions_qs, f)
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
            protein_passes(pa, f, tissue_pks) and protein_passes(pb, f, tissue_pks)
        ):
            continue

        ua = protein_display(pa, isoform_uid_map.get(pa_pk))
        ub = protein_display(pb, isoform_uid_map.get(pb_pk))
        found_results.append(
            pair_row(
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
                pair_not_found(
                    input_a,
                    input_b,
                    input_order,
                    is_noninteraction=False,
                    ua=protein_display(protein_a),
                    ub=protein_display(protein_b),
                )
            ]
        return [resolve_interaction_pair(input_a, input_b, input_order, f, tissue_pks)]

    return found_results


# ---------------------------------------------------------------------------
# Batch entry point
# ---------------------------------------------------------------------------


def check_pairs(pairs: list[tuple[str, str, int]], f: CommonFilters) -> list[dict]:
    """Resolve a batch of ``(input_a, input_b, input_order)`` triples.

    Dispatches per pair across the interaction / non-interaction legs selected
    by ``f.show`` and the isoform mode. One input pair produces exactly one row
    unless isoform expansion found several real combinations: a found row always
    supersedes the not-found fallback, so a pair never returns both.

    Callers enforce their own batch-size cap (``BATCH_LIMIT`` on the web
    endpoint) before calling this.
    """
    expand_isoforms = f.isoform_mode in ("isoforms", "both")
    tissue_pks = tissue_pk_set(f)
    # Per-call cache so repeated proteins in a batch share isoform lookups.
    isoform_cache: dict[int, list] = {}

    results: list[dict] = []
    for input_a, input_b, input_order in pairs:
        if expand_isoforms:
            # Isoform expansion only applies to the Interaction table.
            int_rows: list[dict] = []
            if f.show in ("interactions", "both"):
                int_rows = resolve_interaction_pair_with_isoforms(
                    input_a,
                    input_b,
                    input_order,
                    isoform_cache,
                    f,
                    tissue_pks,
                    isoform_mode=f.isoform_mode,
                )
            nonint_rows: list[dict] = []
            if f.show in ("noninteractions", "both"):
                nr = resolve_noninteraction_pair(
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
                if f.show == "noninteractions":
                    rows = [
                        resolve_noninteraction_pair(
                            input_a, input_b, input_order, f, tissue_pks
                        )
                    ]
                else:
                    rows = [
                        resolve_interaction_pair(
                            input_a, input_b, input_order, f, tissue_pks
                        )
                    ]
        else:
            if f.show == "interactions":
                rows = [
                    resolve_interaction_pair(
                        input_a, input_b, input_order, f, tissue_pks
                    )
                ]
            elif f.show == "noninteractions":
                rows = [
                    resolve_noninteraction_pair(
                        input_a, input_b, input_order, f, tissue_pks
                    )
                ]
            else:  # both
                int_row = resolve_interaction_pair(
                    input_a, input_b, input_order, f, tissue_pks
                )
                nonint_row = resolve_noninteraction_pair(
                    input_a, input_b, input_order, f, tissue_pks
                )
                found = [r for r in [int_row, nonint_row] if r["score"] >= 0]
                rows = found if found else [int_row]

        results.extend(rows)
    return results
