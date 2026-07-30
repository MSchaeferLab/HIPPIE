"""Filter-vocabulary services: the option lists behind every filter control.

Extracted from ``views.py``. One builder feeds the query pages
(``browse_filter_meta``), the ML-splits page (``_ml_filter_meta``), and the MCP
``list_filter_options`` tool, so all three describe the same vocabulary with the
same grouping and the same counts.
"""

from collections.abc import Callable, Iterable

from ..filter_categories import (
    EXPERIMENT_CATEGORY_ORDER,
    INTERACTION_TYPE_CATEGORY_ORDER,
    SOURCE_CATEGORY_ORDER,
    TISSUE_CATEGORY_ORDER,
    experiment_category,
    interaction_type_category,
    source_category,
    tissue_category,
    tissue_prefix,
)
from ..models import ExperimentType, InteractionType, Source, Tissue


def vocab_options(
    model: type,
    categorise: Callable[[dict], str],
    *,
    count_field: str = "n_connected_interactions",
    subcategorise: Callable[[dict, Iterable[dict]], str] | None = None,
    extra_fields: tuple[str, ...] = (),
    include_fields: tuple[str, ...] = (),
) -> list[dict]:
    """Option dicts for one filter vocabulary, ready for the filter controls.

    Each option carries ``id``, ``name``, ``category`` (from
    ``filter_categories``), an optional ``subcategory`` (a second grouping level
    inside the category), and — when known — ``count`` from ``count_field``.
    Rows nothing references are dropped: they are dead choices that only make
    the list longer to scan.

    The count columns are caches refreshed by ``hippie_update`` /
    ``update_tissue_data``, so they read 0 everywhere on a database that has
    never run them (a fresh deployment, a test fixture). Hiding on a zero count
    would then empty the filter entirely, which is far worse than showing a few
    unused options — so when *no* row has a count, the counter is treated as
    unpopulated: nothing is hidden and no (misleading) zero counts are sent.

    ``subcategorise`` receives the full row list as well as the row, because a
    sub-group is only worth creating when several options share it (see the
    tissue grouping in ``filter_option_lists``).

    ``extra_fields`` are selected so ``categorise`` can read them but are *not*
    put in the option dict — the frontend has no use for a PSI-MI code. Name a
    field in ``include_fields`` as well to have it copied through; the MCP filter
    resolver does that for ``psi_mi_code`` so a caller can filter by code.
    """
    fields = ("id", "name", count_field, *extra_fields)
    rows = list(model.objects.order_by("name").values(*fields))
    have_counts = any(row[count_field] for row in rows)
    kept = [row for row in rows if row[count_field] > 0 or not have_counts]
    options = []
    for row in kept:
        opt = {
            "id": row["id"],
            "name": row["name"],
            "category": categorise(row),
        }
        if have_counts:
            opt["count"] = row[count_field]
        if subcategorise is not None:
            sub = subcategorise(row, kept)
            if sub:
                opt["subcategory"] = sub
        for name in include_fields:
            opt[name] = row[name]
        options.append(opt)
    return options


def filter_option_lists() -> dict:
    """Tissue / source / experiment / interaction-type option lists for the
    filter controls. Shared by ``browse_filter_meta`` (the query pages),
    ``_ml_filter_meta`` (the ML-splits page), and the MCP filter tools.

    Every list is limited to options something actually references, and carries a
    ``count`` plus a ``category`` the frontend renders as collapsible groups.
    Tissues additionally carry a ``subcategory`` — their GTEx organ prefix — so
    the 13 brain regions collapse behind one sub-heading instead of filling the
    Nervous system group.
    """

    def _tissue_subcategory(row: dict, rows: Iterable[dict]) -> str:
        """Organ prefix, but only where the organ has more than one tissue.

        A lone organ ("Lung", "Pituitary") would otherwise get a sub-heading
        wrapping a single checkbox; those sit directly under their body system.
        """
        prefix = tissue_prefix(row["name"])
        if sum(1 for r in rows if tissue_prefix(r["name"]) == prefix) < 2:
            return ""
        return prefix

    return {
        "tissues": vocab_options(
            Tissue,
            lambda row: tissue_category(row["name"]),
            count_field="n_expressed_genes",
            subcategorise=_tissue_subcategory,
        ),
        "sources": vocab_options(Source, lambda row: source_category(row["name"])),
        "experiments": vocab_options(
            ExperimentType,
            lambda row: experiment_category(row["psi_mi_code"], row["name"]),
            extra_fields=("psi_mi_code",),
        ),
        "interaction_types": vocab_options(
            InteractionType, lambda row: interaction_type_category(row["name"])
        ),
        # Display order for the group headers, so the frontend does not have to
        # hardcode a copy of the category vocabulary.
        "category_order": {
            "tissues": list(TISSUE_CATEGORY_ORDER),
            "sources": list(SOURCE_CATEGORY_ORDER),
            "experiments": list(EXPERIMENT_CATEGORY_ORDER),
            "interaction_types": list(INTERACTION_TYPE_CATEGORY_ORDER),
        },
    }
