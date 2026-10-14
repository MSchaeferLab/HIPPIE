"""Context for the documentation pages (information + filter guide).

The vocabulary lists come from ``vocab.vocab_options`` so the guide describes
exactly the options the filter controls offer, with the same grouping and the
same (precomputed) counts. Everything here reads small vocabulary tables only.
"""

from collections.abc import Iterable

from ..filter_categories import (
    EXPERIMENT_CATEGORY_ORDER,
    INTERACTION_TYPE_CATEGORY_ORDER,
    SOURCE_CATEGORY_ORDER,
    TISSUE_CATEGORY_ORDER,
    experiment_category,
)
from ..models import ExperimentType, Source
from ..source_links import source_info
from .vocab import filter_option_lists, vocab_options


def group_by_category(
    options: Iterable[dict[str, object]], order: Iterable[str]
) -> list[dict[str, object]]:
    """``[{"category", "options"}]`` in display order; unknown categories last."""
    groups: dict[str, list[dict[str, object]]] = {}
    for opt in options:
        groups.setdefault(str(opt["category"]), []).append(opt)
    ordered = [c for c in order if c in groups]
    ordered += sorted(c for c in groups if c not in ordered)
    return [{"category": c, "options": groups[c]} for c in ordered]


def source_table() -> list[dict[str, object]]:
    """Sources that back at least one interaction, largest first.

    ``n_connected_interactions`` is the cached count refreshed by
    ``hippie_update``, so this is one read of the small ``source`` table.
    """
    rows = Source.objects.filter(n_connected_interactions__gt=0).order_by(
        "-n_connected_interactions", "name"
    )
    return [
        {
            "name": s.name,
            "url": s.url,
            "count": s.n_connected_interactions,
            **source_info(s.name),
        }
        for s in rows
    ]


def filter_guide_context() -> dict[str, object]:
    """Grouped vocabularies for the filter guide page."""
    lists = filter_option_lists()
    # The shared option lists drop psi_mi_code; the guide links each method to
    # its PSI-MI term, so experiments are fetched with the code included.
    experiments = vocab_options(
        ExperimentType,
        lambda row: experiment_category(row["psi_mi_code"], row["name"]),
        extra_fields=("psi_mi_code",),
        include_fields=("psi_mi_code",),
    )
    sources = [{**opt, **source_info(str(opt["name"]))} for opt in lists["sources"]]
    return {
        "source_groups": group_by_category(sources, SOURCE_CATEGORY_ORDER),
        "experiment_groups": group_by_category(experiments, EXPERIMENT_CATEGORY_ORDER),
        "interaction_type_groups": group_by_category(
            lists["interaction_types"], INTERACTION_TYPE_CATEGORY_ORDER
        ),
        "tissue_groups": group_by_category(lists["tissues"], TISSUE_CATEGORY_ORDER),
    }
