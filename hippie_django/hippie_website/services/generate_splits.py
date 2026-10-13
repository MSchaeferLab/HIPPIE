"""Filter semantics for the ML-splits page.

The splitting itself no longer lives here: it is the Nextflow
ppi-splitting-pipeline's job (see services/nextflow_runner.py). What stayed is
the part that defines *which interactions* a split is cut from — the filter
dataclass and the two querysets built from it — because the stats endpoint, the
ppis.csv exporter and the page's own preview box all have to agree on it. A
divergence there would mean the "Calculate Statistics" box describes a different
dataset from the one the pipeline receives.

The module name is kept despite the narrower job: ``SplitParams`` and
``build_interaction_queryset`` are imported from here by views, tasks, the
exporter and the existing tests, and renaming the module buys nothing.
"""

from dataclasses import dataclass

from hippie_website.query_filters import (
    apply_interaction_level_filters,
    apply_protein_level_filters,
    isoform_only_q,
)


@dataclass(frozen=True)
class SplitParams:
    # ── Interaction-level filters (gate which edges are allowed) ──────────
    min_score: float = 0.0
    max_score: float = 1.0
    source_ids: tuple = ()
    experiment_ids: tuple = ()
    type_ids: tuple = ()

    # ── Protein-level filters (gate which nodes are allowed) ──────────────
    # An interaction survives only if BOTH endpoints pass these.
    tissue_ids: tuple = ()
    min_tpm: float = 0.0
    # Gates the GLOBAL Protein.degree column — degree across all of HIPPIE, not
    # degree within the subgraph left by the interaction-level filters above.
    # Named for that distinction because the two diverge sharply under a narrow
    # source filter, and the stats box reports the *subgraph* degree.
    min_degree_global: int = 0
    min_avg_score: float = 0.0
    isoform_mode: str = "general"  # general | isoforms | both

    # ── Negative sampling (historical) ────────────────────────────────────
    # Neither reaches the pipeline any more: it fixes the ratio per split (1:1
    # for train/val/test_balanced, 1:10 for test_realistic) and takes the seed
    # from conf/hippie.config. The fields stay because SplitJob.params rows
    # written before the switch still carry both keys, and from_payload() has to
    # accept them for those jobs to remain readable on the status endpoint.
    neg_ratio: float = 1.0
    seed: int = 78539105873

    @classmethod
    def from_payload(cls, payload: dict) -> "SplitParams":
        """Build from a stored/POSTed param dict, tolerating the legacy key.

        ``SplitJob.params`` rows written before ``min_degree`` was renamed to
        ``min_degree_global`` are still on disk and still replayable, so the old
        key is mapped rather than rejected.
        """
        data = dict(payload)
        if "min_degree" in data:
            data.setdefault("min_degree_global", data.pop("min_degree"))
            data.pop("min_degree", None)
        return cls(**data)


def allowed_protein_id_qs(params: SplitParams):
    """
    Return a lazy ``values("pk")`` queryset of Protein PKs passing the
    protein-level filters (tissue expression, min degree, min avg score), or
    ``None`` when no protein-level filter is active (so callers can skip node
    gating entirely).

    ``degree`` / ``avg_score`` are the denormalised columns on Protein
    (refreshed by ``recompute_protein_stats``) — global values, matching the
    Browse page. Isoform inclusion is handled at the *edge* level via the
    ``involves_isoform`` flag, so it is intentionally not applied here.

    Note the asymmetry this creates, which the UI labels spell out: a protein
    passes on its degree across ALL of HIPPIE, but the resulting subgraph is then
    cut down by the interaction-level filters, so surviving nodes can end up with
    a far lower degree inside the split than the threshold suggests.
    """
    from hippie_website.models import Protein

    active = (
        bool(params.tissue_ids)
        or params.min_degree_global > 0
        or params.min_avg_score > 0
    )
    if not active:
        return None

    qs = apply_protein_level_filters(
        Protein.objects.all(),
        tissue_ids=params.tissue_ids,
        min_tpm=params.min_tpm,
        min_degree=params.min_degree_global,
        min_avg_score=params.min_avg_score,
    )
    return qs.values("pk")


def build_interaction_queryset(params: SplitParams):
    """
    Build the filtered Interaction queryset shared by the graph builder and the
    statistics endpoint. Applies interaction-level filters (score range,
    sources, experiments, types), isoform exclusion, and protein-level node
    gating (both endpoints must pass the protein filters).

    M2M filters use ``Exists`` over the through tables so rows are never
    multiplied — keeping ``.count()`` accurate and edge iteration duplicate-free.
    """
    from hippie_website.models import Interaction

    # ── Interaction-level filters (shared with the query pages) ──────────
    # Pass score bounds only when they actually constrain, preserving the
    # previous ``above_score`` / ``score__lte`` behaviour (no no-op WHERE).
    qs = apply_interaction_level_filters(
        Interaction.objects.all(),
        min_score=params.min_score if params.min_score > 0 else None,
        max_score=params.max_score if params.max_score < 1.0 else None,
        source_ids=list(params.source_ids),
        experiment_ids=list(params.experiment_ids),
        type_ids=list(params.type_ids),
    )

    # ── Isoform handling (denormalised flag — one indexed column) ────────
    if params.isoform_mode == "general":
        qs = qs.filter(involves_isoform=False)
    elif params.isoform_mode == "isoforms":
        qs = qs.filter(involves_isoform=True)

    # ── Protein-level node gating ────────────────────────────────────────
    pid_qs = allowed_protein_id_qs(params)
    if pid_qs is not None:
        qs = qs.filter(protein_1_id__in=pid_qs, protein_2_id__in=pid_qs)

    return qs


def build_noninteraction_queryset(params: SplitParams):
    """
    The curated-negatives counterpart to ``build_interaction_queryset``, for the
    "Download raw data" package (see services/raw_export.py). Returns ``None``
    when a source/experiment/type filter is active — a ``NonInteraction`` carries
    none of that evidence, so restricting by it can never mean anything (same
    convention as ``CommonFilters.has_source_like`` / ``noninteraction_edge_qs``
    in services/queries.py).

    Otherwise: the same score bounds and protein-level node gating as the
    positives, plus an isoform-mode gate computed inline — unlike Interaction,
    NonInteraction has no denormalised ``involves_isoform`` column.
    """
    from hippie_website.models import NonInteraction

    if params.source_ids or params.experiment_ids or params.type_ids:
        return None

    qs = NonInteraction.objects.all()
    if params.min_score > 0:
        qs = qs.filter(score__gte=params.min_score)
    if params.max_score < 1.0:
        qs = qs.filter(score__lte=params.max_score)

    if params.isoform_mode == "general":
        qs = qs.filter(protein_1__isoform__isnull=True, protein_2__isoform__isnull=True)
    elif params.isoform_mode == "isoforms":
        qs = qs.filter(isoform_only_q())

    pid_qs = allowed_protein_id_qs(params)
    if pid_qs is not None:
        qs = qs.filter(protein_1_id__in=pid_qs, protein_2_id__in=pid_qs)

    return qs
