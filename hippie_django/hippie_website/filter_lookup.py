"""Resolve human-written filter values to vocabulary primary keys.

The filter contract in ``services.queries`` takes integer PKs: ``source_ids``,
``experiment_ids``, ``interaction_type_ids``, ``tissue_ids``. That is right for
the frontend, which has the whole vocabulary in hand and sends back what the
user ticked. It is wrong for anything writing a query by hand — an agent, a
script, a curl — because ``experiment=17`` is unguessable and the display names
are long PSI-MI phrases.

This module closes that gap. It accepts what a caller would plausibly write and
resolves it against the same option lists the filter controls show, in five
tiers, first match wins:

1. an integer (or digit string) — a PK, verified to exist
2. an exact case-insensitive name
3. a PSI-MI code (experiment types only, e.g. ``MI:0018``)
4. an exact :mod:`filter_categories` **group label** — expands to every member
   of that category, so ``"Two-hybrid & complementation"`` stands in for the
   couple of dozen individual PSI-MI terms underneath it
5. a unique case-insensitive substring of a name

Anything left over is reported as unresolved with near-miss candidates, never
silently dropped: a typo that quietly resolved to nothing would look exactly
like a protein pair with no evidence.

Resolution runs against ``vocab.vocab_options`` — the same list
``list_filter_options`` exposes — so a caller can never filter on a term the
tool would not have shown them.
"""

import difflib
from collections.abc import Callable
from dataclasses import dataclass, field

from .filter_categories import (
    EXPERIMENT_CATEGORY_ORDER,
    INTERACTION_TYPE_CATEGORY_ORDER,
    SOURCE_CATEGORY_ORDER,
    TISSUE_CATEGORY_ORDER,
    experiment_category,
    interaction_type_category,
    source_category,
    tissue_category,
)
from .models import ExperimentType, InteractionType, Source, Tissue
from .services.vocab import vocab_options

KIND_SOURCE = "source"
KIND_EXPERIMENT = "experiment"
KIND_INTERACTION_TYPE = "interaction_type"
KIND_TISSUE = "tissue"

KINDS: tuple[str, ...] = (
    KIND_SOURCE,
    KIND_EXPERIMENT,
    KIND_INTERACTION_TYPE,
    KIND_TISSUE,
)

# How many near-miss suggestions to offer for an unresolved value. Enough to be
# useful, few enough not to bloat a tool result.
_MAX_CANDIDATES = 8


@dataclass(frozen=True)
class _VocabSpec:
    """Where one vocabulary's options come from, and how they are categorised."""

    model: type
    categorise: Callable[[dict], str]
    count_field: str
    extra_fields: tuple[str, ...]
    category_order: tuple[str, ...]
    # The CommonFilters field this vocabulary populates.
    filter_field: str


_SPECS: dict[str, _VocabSpec] = {
    KIND_SOURCE: _VocabSpec(
        model=Source,
        categorise=lambda row: source_category(row["name"]),
        count_field="n_connected_interactions",
        extra_fields=(),
        category_order=SOURCE_CATEGORY_ORDER,
        filter_field="source_ids",
    ),
    KIND_EXPERIMENT: _VocabSpec(
        model=ExperimentType,
        categorise=lambda row: experiment_category(row["psi_mi_code"], row["name"]),
        count_field="n_connected_interactions",
        extra_fields=("psi_mi_code",),
        category_order=EXPERIMENT_CATEGORY_ORDER,
        filter_field="experiment_ids",
    ),
    KIND_INTERACTION_TYPE: _VocabSpec(
        model=InteractionType,
        categorise=lambda row: interaction_type_category(row["name"]),
        count_field="n_connected_interactions",
        extra_fields=(),
        category_order=INTERACTION_TYPE_CATEGORY_ORDER,
        filter_field="interaction_type_ids",
    ),
    KIND_TISSUE: _VocabSpec(
        model=Tissue,
        categorise=lambda row: tissue_category(row["name"]),
        count_field="n_expressed_genes",
        extra_fields=(),
        category_order=TISSUE_CATEGORY_ORDER,
        filter_field="tissue_ids",
    ),
}


def filter_field_for(kind: str) -> str:
    """Name of the :class:`~.services.queries.CommonFilters` field ``kind`` fills."""
    return _spec(kind).filter_field


def _spec(kind: str) -> _VocabSpec:
    try:
        return _SPECS[kind]
    except KeyError:
        raise ValueError(
            f"Unknown filter kind {kind!r}. Expected one of: {', '.join(KINDS)}."
        ) from None


def options_for(kind: str) -> list[dict]:
    """The option dicts for one vocabulary: ``id``, ``name``, ``category``,
    optional ``count`` and (experiments) ``psi_mi_code``."""
    spec = _spec(kind)
    return vocab_options(
        spec.model,
        spec.categorise,
        count_field=spec.count_field,
        extra_fields=spec.extra_fields,
        # Expose the same extras (experiments' psi_mi_code) so a caller can
        # filter by code, and so list_filter_options can show it.
        include_fields=spec.extra_fields,
    )


def category_order_for(kind: str) -> list[str]:
    """Display order of ``kind``'s *declared* category labels, "Other" pinned last.

    Declared, not populated: the list is a static tuple in ``filter_categories``
    and can name a category the database currently has no rows for. Anything
    caller-facing wants :func:`populated_category_order_for` instead.
    """
    return list(_spec(kind).category_order)


def populated_category_order_for(
    kind: str, *, options: list[dict] | None = None
) -> list[str]:
    """:func:`category_order_for`, minus categories with no live options.

    A category label is only worth showing if filtering by it would select
    something. Pass ``options`` to reuse an option list already fetched, which is
    what :mod:`hippie_mcp.server` does — otherwise this costs one query.
    """
    opts = options if options is not None else options_for(kind)
    present = {o["category"] for o in opts}
    return [label for label in category_order_for(kind) if label in present]


@dataclass
class FilterMatch:
    """One input value and what it resolved to."""

    value: str
    matched_by: str  # id | name | psi_mi_code | category | substring
    ids: list[int] = field(default_factory=list)
    names: list[str] = field(default_factory=list)


@dataclass
class FilterUnresolved:
    """One input value that matched nothing, with near-miss suggestions."""

    value: str
    reason: str  # unknown | ambiguous
    candidates: list[str] = field(default_factory=list)


@dataclass
class FilterResolution:
    """Outcome of resolving one vocabulary's worth of input values."""

    kind: str
    ids: list[int] = field(default_factory=list)
    matched: list[FilterMatch] = field(default_factory=list)
    unresolved: list[FilterUnresolved] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when every input value resolved to at least one option."""
        return not self.unresolved

    def echo(self) -> dict:
        """Compact, caller-facing summary of what happened.

        Tools return this alongside their results so the caller can see that
        "Two-hybrid & complementation" became 23 experiment types, and that
        "yeast2hybrid" matched nothing but looks close to these three.
        """
        out: dict = {}
        if self.matched:
            out["matched"] = [
                {
                    "input": m.value,
                    "matched_by": m.matched_by,
                    "n_matched": len(m.ids),
                    # A category expansion can be long; show enough to confirm
                    # the expansion was what the caller meant.
                    "names": m.names[:_MAX_CANDIDATES],
                    **(
                        {"names_truncated": True}
                        if len(m.names) > _MAX_CANDIDATES
                        else {}
                    ),
                }
                for m in self.matched
            ]
        if self.unresolved:
            out["unresolved"] = [
                {
                    "input": u.value,
                    "reason": u.reason,
                    "did_you_mean": u.candidates,
                }
                for u in self.unresolved
            ]
        return out


def resolve_filter_values(
    kind: str,
    values: list[str | int] | None,
    *,
    options: list[dict] | None = None,
) -> FilterResolution:
    """Resolve ``values`` for one vocabulary to primary keys.

    ``kind`` is one of :data:`KINDS`. ``values`` may mix ids, names, PSI-MI
    codes, category labels, and substrings; an empty or ``None`` list resolves
    to an empty result rather than an error. Pass ``options`` to reuse an option
    list already fetched (one DB query per vocabulary otherwise).

    The returned ``ids`` are deduplicated and order-preserving. Resolution never
    raises for a bad *value* — bad values land in ``unresolved`` — but an unknown
    ``kind`` is a programming error and raises ``ValueError``.
    """
    spec = _spec(kind)
    resolution = FilterResolution(kind=kind)
    if not values:
        return resolution

    opts = options if options is not None else options_for(kind)

    by_id: dict[int, dict] = {o["id"]: o for o in opts}
    by_name: dict[str, dict] = {o["name"].casefold(): o for o in opts}
    by_code: dict[str, dict] = {
        o["psi_mi_code"].casefold(): o for o in opts if o.get("psi_mi_code")
    }
    # Built from the live options only. A category the vocabulary *declares* but
    # has no rows for is deliberately absent, so it falls through to unresolved
    # instead of matching and expanding to nothing: an empty expansion is
    # indistinguishable from "no filter", which would silently widen the query.
    # Such a category is not listed by list_filter_options either.
    by_category: dict[str, list[dict]] = {}
    for o in opts:
        by_category.setdefault(o["category"].casefold(), []).append(o)

    # Spelling suggestions offer only categories that exist, for the same reason.
    populated_categories = [
        label for label in spec.category_order if label.casefold() in by_category
    ]

    seen_ids: set[int] = set()

    def _take(match: FilterMatch) -> None:
        resolution.matched.append(match)
        for pk in match.ids:
            if pk not in seen_ids:
                seen_ids.add(pk)
                resolution.ids.append(pk)

    for raw in values:
        text = str(raw).strip()
        if not text:
            continue
        folded = text.casefold()

        # 1) integer PK
        if isinstance(raw, int) or text.isdigit():
            pk = int(text)
            opt = by_id.get(pk)
            if opt is not None:
                _take(FilterMatch(text, "id", [pk], [opt["name"]]))
            else:
                resolution.unresolved.append(
                    FilterUnresolved(text, "unknown", candidates=[])
                )
            continue

        # 2) exact name
        opt = by_name.get(folded)
        if opt is not None:
            _take(FilterMatch(text, "name", [opt["id"]], [opt["name"]]))
            continue

        # 3) PSI-MI code (experiment types)
        opt = by_code.get(folded)
        if opt is not None:
            _take(FilterMatch(text, "psi_mi_code", [opt["id"]], [opt["name"]]))
            continue

        # 4) category label → every member of the category
        if folded in by_category:
            members = by_category[folded]
            _take(
                FilterMatch(
                    text,
                    "category",
                    [m["id"] for m in members],
                    [m["name"] for m in members],
                )
            )
            continue

        # 5) unique substring
        hits = [o for o in opts if folded in o["name"].casefold()]
        if len(hits) == 1:
            _take(FilterMatch(text, "substring", [hits[0]["id"]], [hits[0]["name"]]))
            continue
        if len(hits) > 1:
            resolution.unresolved.append(
                FilterUnresolved(
                    text,
                    "ambiguous",
                    candidates=[o["name"] for o in hits[:_MAX_CANDIDATES]],
                )
            )
            continue

        resolution.unresolved.append(
            FilterUnresolved(
                text,
                "unknown",
                candidates=difflib.get_close_matches(
                    text,
                    [o["name"] for o in opts] + populated_categories,
                    n=_MAX_CANDIDATES,
                    cutoff=0.5,
                ),
            )
        )

    return resolution


def resolve_all(
    values_by_kind: dict[str, list[str | int] | None],
) -> dict[str, FilterResolution]:
    """Resolve several vocabularies at once, one query per non-empty kind.

    Kinds whose value list is empty or ``None`` are skipped entirely, so the
    common case of "filter by score only" costs no vocabulary queries.
    """
    out: dict[str, FilterResolution] = {}
    for kind, values in values_by_kind.items():
        if not values:
            continue
        out[kind] = resolve_filter_values(kind, values)
    return out
