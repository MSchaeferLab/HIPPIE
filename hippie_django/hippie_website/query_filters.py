"""
Shared query-building helpers for the HIPPIE query pages, browse endpoints, the
ML-splits service, and the denormalisation signals/commands.

Extracted so the interaction-level filter, the protein-level node gate, the
canonical-or-queried isoform predicate, the per-FK-side aggregation, and the
evidence-count recompute each live in exactly one place. Every function emits
the same SQL as the code it replaced: these ride the documented
``(protein_1, score)`` / ``(protein_2, score)`` covering indexes and the
``EXISTS``-over-through-table pattern, and none of them annotate a queryset that
is later ``.count()``-ed.
"""

from django.db.models import Count, Exists, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce


def canonical_or_queried_q(protein_pks) -> Q:
    """Keep an edge only when each endpoint is canonical (not an isoform) OR is
    one of the explicitly queried proteins. An FK anti-join used across the
    query pages instead of a ``NOT IN (isoform-pk)`` subquery ORed over both
    sides."""
    return (Q(protein_1__isoform__isnull=True) | Q(protein_1_id__in=protein_pks)) & (
        Q(protein_2__isoform__isnull=True) | Q(protein_2_id__in=protein_pks)
    )


def apply_interaction_level_filters(
    qs,
    *,
    min_score=None,
    max_score=None,
    source_ids=(),
    experiment_ids=(),
    type_ids=(),
):
    """Apply score / source / experiment / interaction-type filters to an
    Interaction queryset using ``EXISTS`` over the indexed M2M through tables
    (rows are never multiplied, so ``.count()`` stays accurate)."""
    model = qs.model
    if min_score is not None:
        qs = qs.filter(score__gte=min_score)
    if max_score is not None:
        qs = qs.filter(score__lte=max_score)
    if source_ids:
        qs = qs.filter(
            Exists(
                model.sources.through.objects.filter(
                    interaction_id=OuterRef("pk"), source_id__in=source_ids
                )
            )
        )
    if experiment_ids:
        qs = qs.filter(
            Exists(
                model.experiments.through.objects.filter(
                    interaction_id=OuterRef("pk"),
                    experimenttype_id__in=experiment_ids,
                )
            )
        )
    if type_ids:
        qs = qs.filter(
            Exists(
                model.interaction_types.through.objects.filter(
                    interaction_id=OuterRef("pk"), interactiontype_id__in=type_ids
                )
            )
        )
    return qs


def apply_protein_level_filters(
    qs,
    *,
    tissue_ids=(),
    min_rpkm=0.0,
    min_degree=0,
    min_avg_score=0.0,
):
    """Gate a Protein queryset by tissue expression, degree, and average score.

    ``degree`` / ``avg_score`` are the denormalised indexed scalar columns on
    Protein (refreshed by ``recompute_protein_stats``). A 0 / empty value
    disables that gate. Callers that also gate by ``reviewed`` / isoform apply
    those separately.
    """
    if tissue_ids:
        qs = qs.expressed_in(
            list(tissue_ids),
            min_rpkm=min_rpkm if min_rpkm and min_rpkm > 0 else None,
        )
    if min_degree and min_degree > 0:
        qs = qs.filter(degree__gte=min_degree)
    if min_avg_score and min_avg_score > 0:
        qs = qs.filter(avg_score__gte=min_avg_score)
    return qs


def group_by_side(qs, col: str) -> dict[int, tuple[int, float]]:
    """``{protein_pk: (interaction_count, score_sum)}`` grouped by one FK side.

    One GROUP BY riding the ``(protein_1, score)`` / ``(protein_2, score)``
    covering indexes — shared by ``recompute_protein_stats`` and the ML-splits
    stats box.
    """
    return {
        row[col]: (row["cnt"], row["sm"] or 0.0)
        for row in qs.values(col).annotate(cnt=Count("id"), sm=Sum("score"))
    }


def edge_count_subquery(through):
    """Correlated per-interaction edge count over one M2M through table."""
    return Coalesce(
        Subquery(
            through.objects.filter(interaction_id=OuterRef("pk"))
            .values("interaction_id")
            .annotate(c=Count("pk"))
            .values("c")
        ),
        0,
    )


def recompute_evidence_counts(interaction_qs) -> None:
    """Refresh ``Interaction.n_sources`` / ``n_experiments`` from the M2M through
    tables for the given queryset (all rows after a bulk import, or a
    ``pk__in`` slice from the admin-edit signals)."""
    model = interaction_qs.model
    interaction_qs.update(
        n_sources=edge_count_subquery(model.sources.through),
        n_experiments=edge_count_subquery(model.experiments.through),
    )
