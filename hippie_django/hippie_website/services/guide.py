"""Context for the documentation pages (information + filter guide).

The vocabulary lists come from ``vocab.vocab_options`` so the guide describes
exactly the options the filter controls offer, with the same grouping and the
same (precomputed) counts. Everything here reads small vocabulary tables only.
"""

from collections.abc import Iterable

from ..filter_categories import (
    ANNOTATION_DB,
    ARCHIVE_DB,
    CURATION_TEAM,
    EXPERIMENT_CATEGORY_ORDER,
    INTERACTION_DB,
    INTERACTION_TYPE_CATEGORY_ORDER,
    LITERATURE_DB,
    OTHER,
    SOURCE_CATEGORY_ORDER,
    TISSUE_CATEGORY_ORDER,
    experiment_category,
    source_category,
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


# (singular, plural) noun phrase per source category, for the one-line summary.
_CATEGORY_NOUNS: dict[str, tuple[str, str]] = {
    ANNOTATION_DB: ("curated annotation resource", "curated annotation resources"),
    CURATION_TEAM: ("curation team", "curation teams"),
    INTERACTION_DB: ("interaction database", "interaction databases"),
    LITERATURE_DB: ("literature resource", "literature resources"),
    ARCHIVE_DB: (
        "structural & proteomics archive",
        "structural & proteomics archives",
    ),
    OTHER: ("other source", "other sources"),
}


def source_category_summary(names: Iterable[str]) -> str:
    """``"1 curated annotation resource, 4 curation teams, 10 interaction databases"``.

    Counts the given source names per filter category, in the filter's category
    order, skipping empty categories.
    """
    counts: dict[str, int] = {}
    for name in names:
        category = source_category(name)
        counts[category] = counts.get(category, 0) + 1
    parts = []
    for category in SOURCE_CATEGORY_ORDER:
        n = counts.get(category, 0)
        if n:
            singular, plural = _CATEGORY_NOUNS[category]
            parts.append(f"{n} {singular if n == 1 else plural}")
    return ", ".join(parts)


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
            "full_name": s.description,
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
    descriptions = dict(Source.objects.values_list("name", "description"))
    sources = [
        {
            **opt,
            **source_info(str(opt["name"])),
            "full_name": descriptions.get(str(opt["name"]), ""),
        }
        for opt in lists["sources"]
    ]
    return {
        "source_groups": group_by_category(sources, SOURCE_CATEGORY_ORDER),
        "experiment_groups": group_by_category(experiments, EXPERIMENT_CATEGORY_ORDER),
        "interaction_type_groups": group_by_category(
            lists["interaction_types"], INTERACTION_TYPE_CATEGORY_ORDER
        ),
        "tissue_groups": group_by_category(lists["tissues"], TISSUE_CATEGORY_ORDER),
    }
