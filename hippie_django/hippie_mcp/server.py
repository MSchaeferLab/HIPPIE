"""The HIPPIE MCP server: five read-only tools over the interaction database.

Each tool is a thin adapter over ``hippie_website.services``, so a query issued
here means exactly what the same query means on the website. Tools are declared
``def`` rather than ``async def`` on purpose: the SDK runs a synchronous tool in
a worker thread, which is what makes the synchronous Django ORM legal — see
:mod:`hippie_mcp.db` for the connection hygiene that requires.

Filter arguments accept names, PSI-MI codes, ``filter_categories`` group labels,
or raw ids, resolved by :mod:`hippie_website.filter_lookup`. Every result echoes
what the filters resolved to, and a filter value that matches nothing fails the
call instead of quietly widening the query.
"""

from typing import Annotated, Literal

from django.http import Http404
from django.urls import reverse
from mcp.server import MCPServer
from pydantic import Field

from hippie_website import filter_lookup
from hippie_website.models import Protein
from hippie_website.services import detail as detail_service
from hippie_website.services import pairs as pairs_service
from hippie_website.services import queries as query_service
from hippie_website.services.queries import CommonFilters

from .db import with_db
from .shaping import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    absolute_url,
    clamp_limit,
    interactions_result,
    pairs_result,
)

INSTRUCTIONS = """\
HIPPIE is a database of human protein-protein interactions, each scored 0-1 by
the amount and quality of its experimental evidence.

Typical flow: `resolve_protein` to pin down an ambiguous identifier, then
`get_interactions` for a protein's partners or `check_pairs` for specific pairs,
then `get_interaction_detail` for the PMIDs and methods behind one interaction.

Filters (sources, experiments, interaction_types, tissues) accept plain names,
PSI-MI codes, or a category label such as "Two-hybrid & complementation" that
stands in for every method underneath it. Call `list_filter_options` to see the
vocabulary. Scores are comparable across the whole database; 0.63 and above is
the commonly used high-confidence cutoff.
"""

mcp = MCPServer(
    "HIPPIE",
    title="HIPPIE protein-protein interactions",
    description="Human protein-protein interaction data with confidence scores.",
    instructions=INSTRUCTIONS,
)


# ---------------------------------------------------------------------------
# Shared argument handling
# ---------------------------------------------------------------------------

FilterValues = Annotated[
    list[str] | None,
    Field(
        default=None,
        description=(
            "Names, PSI-MI codes, category labels, or numeric ids. A category "
            "label expands to every member of that category. Call "
            "list_filter_options to see what is available."
        ),
    ),
]

ScoreValue = Annotated[
    float | None,
    Field(default=None, ge=0.0, le=1.0, description="Confidence score, 0-1."),
]


def _resolve_filters(
    sources: list[str] | None,
    experiments: list[str] | None,
    interaction_types: list[str] | None,
    tissues: list[str] | None,
) -> tuple[dict, dict, list[str]]:
    """Resolve every supplied vocabulary filter to primary keys.

    Returns ``(ids_by_filter_field, echo, problems)``. ``problems`` is non-empty
    when a value matched nothing: the caller must not run the query, because an
    unresolved filter collapses to "no filter" and would silently return a wider
    result set than was asked for.
    """
    resolutions = filter_lookup.resolve_all(
        {
            filter_lookup.KIND_SOURCE: sources,
            filter_lookup.KIND_EXPERIMENT: experiments,
            filter_lookup.KIND_INTERACTION_TYPE: interaction_types,
            filter_lookup.KIND_TISSUE: tissues,
        }
    )

    ids: dict[str, list[int]] = {}
    echo: dict[str, dict] = {}
    problems: list[str] = []
    for kind, resolution in resolutions.items():
        ids[filter_lookup.filter_field_for(kind)] = resolution.ids
        echo[kind] = resolution.echo()
        for unresolved in resolution.unresolved:
            hint = (
                f" Did you mean: {', '.join(unresolved.candidates)}?"
                if unresolved.candidates
                else ""
            )
            problems.append(
                f"{kind}: {unresolved.value!r} matched nothing ({unresolved.reason})."
                f"{hint}"
            )
    return ids, echo, problems


def _filter_error(problems: list[str], echo: dict) -> dict:
    return {
        "summary": (
            "Query not run — one or more filter values could not be resolved. "
            + " ".join(problems)
        ),
        "error": "unresolved_filter",
        "problems": problems,
        "resolved_filters": echo,
    }


def _query_url(identifiers: list[str]) -> str:
    return absolute_url(f"{reverse('hippie_website:index')}?q={'+'.join(identifiers)}")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
@with_db
def resolve_protein(
    identifier: Annotated[
        str,
        Field(
            description=(
                "A gene symbol (TP53), UniProt accession (P04637), UniProt "
                "entry name (P53_HUMAN), Entrez gene id (7157), isoform "
                "accession (P04637-2), or a known synonym."
            )
        ),
    ],
) -> dict[str, object]:
    """Look up which HIPPIE protein an identifier refers to.

    Use this first when an identifier is ambiguous or user-supplied, so the
    later queries name a protein the database actually has. Reports the
    protein's global degree (its total number of interaction partners) and mean
    interaction score, which are useful for judging how much a result set will
    contain before asking for it.
    """
    resolved = Protein.objects.resolve(identifier).select_related("gene")
    # One extra row is enough to know the cap was hit without counting the rest.
    window = list(resolved[: MAX_LIMIT + 1])
    matches = window[:MAX_LIMIT]
    truncated = len(window) > MAX_LIMIT
    total = resolved.count() if truncated else len(matches)
    if not matches:
        return {
            "summary": (
                f"No HIPPIE protein matches {identifier!r}. HIPPIE covers human "
                f"proteins only; check the identifier or try a gene symbol."
            ),
            "found": False,
            "total": 0,
            "returned": 0,
            "truncated": False,
            "matches": [],
        }

    isoform_pks = set(
        query_service.Isoform.objects.filter(
            protein_ptr_id__in=[p.pk for p in matches]
        ).values_list("protein_ptr_id", flat=True)
    )

    rows = [
        {
            "symbol": p.gene.entrez_name or p.uniprot_name,
            "uniprot_id": p.uniprot_accession,
            "uniprot_name": p.uniprot_name,
            "entrez_id": p.gene.entrez_id or None,
            "is_reviewed": p.is_reviewed,
            "is_isoform": p.pk in isoform_pks,
            "degree": p.degree,
            "avg_score": round(p.avg_score, 4) if p.avg_score is not None else None,
            "website_url": _query_url([p.uniprot_accession]),
        }
        for p in matches
    ]

    first = rows[0]
    if total == 1:
        summary = (
            f"{identifier!r} is {first['symbol']} ({first['uniprot_id']}), "
            f"with {first['degree']} interaction partners in HIPPIE."
        )
    else:
        summary = (
            f"{identifier!r} matches {total} HIPPIE proteins: "
            + ", ".join(f"{r['symbol']} ({r['uniprot_id']})" for r in rows[:10])
            + ". Pass a UniProt accession to disambiguate."
        )
    if truncated:
        summary += f" Listing {len(rows)} of the {total} matches (cap {MAX_LIMIT})."

    return {
        "summary": summary,
        "found": True,
        "total": total,
        "returned": len(rows),
        "truncated": truncated,
        "matches": rows,
    }


@mcp.tool()
@with_db
def get_interactions(
    proteins: Annotated[
        list[str],
        Field(
            description=(
                "Protein identifiers to fetch partners for. Any form "
                "resolve_protein accepts. Max "
                f"{query_service.MAX_QUERY_PROTEINS} per call."
            )
        ),
    ],
    min_score: ScoreValue = None,
    max_score: ScoreValue = None,
    sources: FilterValues = None,
    experiments: FilterValues = None,
    interaction_types: FilterValues = None,
    tissues: FilterValues = None,
    min_rpkm: Annotated[
        float | None,
        Field(
            default=None,
            ge=0.0,
            description="Minimum median RPKM in the selected tissues.",
        ),
    ] = None,
    min_degree: Annotated[
        int | None,
        Field(
            default=None,
            ge=0,
            description="Drop partners with fewer than this many total partners.",
        ),
    ] = None,
    min_avg_score: ScoreValue = None,
    reviewed: Annotated[
        Literal["both", "reviewed", "unreviewed"],
        Field(default="both", description="UniProt review status of the partner."),
    ] = "both",
    isoform_mode: Annotated[
        Literal["general", "isoforms", "both"],
        Field(
            default="general",
            description=(
                "general: canonical proteins. isoforms: isoform-level edges only. "
                "both: no isoform filter."
            ),
        ),
    ] = "general",
    show: Annotated[
        Literal["interactions", "noninteractions", "both"],
        Field(
            default="interactions",
            description=(
                "noninteractions returns experimentally supported NON-"
                "interactions (Negatome), which are useful as negative examples."
            ),
        ),
    ] = "interactions",
    limit: Annotated[
        int,
        Field(
            default=DEFAULT_LIMIT,
            ge=1,
            le=MAX_LIMIT,
            description=f"Rows to return, highest score first (max {MAX_LIMIT}).",
        ),
    ] = DEFAULT_LIMIT,
    format: Annotated[
        Literal["summary", "rows"],
        Field(
            default="summary",
            description=(
                "summary adds a prose lead naming the top partners; rows is "
                "terser for bulk use. Both return the same row data."
            ),
        ),
    ] = "summary",
) -> dict[str, object]:
    """Find the interaction partners of one or more proteins.

    The total number of matches is always reported, so a capped result is
    visibly capped rather than looking complete. Scores run 0-1; 0.63 is the
    commonly used high-confidence cutoff.
    """
    tokens = [t for t in (p.strip() for p in proteins) if t]
    if not tokens:
        return {
            "summary": "No protein identifiers supplied.",
            "error": "no_query",
            "total": 0,
            "returned": 0,
            "rows": [],
        }
    if len(tokens) > query_service.MAX_QUERY_PROTEINS:
        return {
            "summary": (
                f"Too many proteins: {len(tokens)} "
                f"(max {query_service.MAX_QUERY_PROTEINS} per call)."
            ),
            "error": "too_many_proteins",
            "total": 0,
            "returned": 0,
            "rows": [],
        }

    ids, echo, problems = _resolve_filters(
        sources, experiments, interaction_types, tissues
    )
    if problems:
        return _filter_error(problems, echo)

    f = CommonFilters.from_dict(
        {
            "show": show,
            "isoform_mode": isoform_mode,
            "min_score": min_score,
            "max_score": max_score,
            "min_rpkm": min_rpkm,
            "min_degree": min_degree,
            "min_avg_score": min_avg_score,
            "reviewed": reviewed,
            **ids,
        }
    )

    found = query_service.resolve_proteins(tokens, f.isoform_mode)
    if not found.found_any:
        return {
            "summary": (
                f"None of the supplied identifiers matched a HIPPIE protein: "
                f"{', '.join(found.unresolved)}."
            ),
            "error": "no_proteins_found",
            "total": 0,
            "returned": 0,
            "rows": [],
            "unresolved_identifiers": found.unresolved,
        }

    # Capped in the query where the filters allow it, so a hub protein does not
    # pay to build thousands of rows for a 25-row answer. The true total comes
    # back alongside, because a capped result must still say what it capped.
    capped = clamp_limit(limit)
    rows, total = query_service.interaction_rows_page(
        found.protein_pks, f, found.isoform_uid_map, limit=capped
    )
    # Name the accession, not just the symbol: a symbol like TP53 matches several
    # HIPPIE records and resolution picks one, so the summary has to say which.
    symbols = [
        f"{d['symbol']} ({d['isoform_uniprot_id'] or d['uniprot_id']})"
        for d in (
            query_service.protein_display(p, found.isoform_uid_map.get(p.pk))
            for p in found.resolved
        )
    ]
    result = interactions_result(
        rows=rows,
        limit=capped,
        total=total,
        query_symbols=symbols,
        unresolved=found.unresolved,
        show=show,
        resolved_filters=echo,
        fmt=format,
    )
    result["website_url"] = _query_url(tokens)
    return result


@mcp.tool()
@with_db
def check_pairs(
    pairs: Annotated[
        list[list[str]],
        Field(
            description=(
                "Pairs to check, each a two-element list of protein "
                f"identifiers. Max {pairs_service.BATCH_LIMIT} per call."
            )
        ),
    ],
    min_score: ScoreValue = None,
    max_score: ScoreValue = None,
    sources: FilterValues = None,
    experiments: FilterValues = None,
    interaction_types: FilterValues = None,
    tissues: FilterValues = None,
    reviewed: Annotated[
        Literal["both", "reviewed", "unreviewed"],
        Field(default="both", description="UniProt review status of both sides."),
    ] = "both",
    isoform_mode: Annotated[
        Literal["general", "isoforms", "both"],
        Field(
            default="general",
            description="Whether to also test isoform combinations of each pair.",
        ),
    ] = "general",
    show: Annotated[
        Literal["interactions", "noninteractions", "both"],
        Field(
            default="both",
            description=(
                "Default 'both' so a pair recorded as a non-interaction is "
                "reported as such rather than as absent."
            ),
        ),
    ] = "both",
) -> dict[str, object]:
    """Check specific protein pairs for a recorded interaction.

    Each pair gets one row with an explicit ``outcome``: ``interacts``,
    ``does_not_interact`` (an experimentally supported non-interaction),
    ``no_record`` (both proteins known, nothing recorded between them), or
    ``unknown_identifier``. Absence of evidence and evidence of absence are
    different answers here, so they are different outcomes.
    """
    # Size is checked against the raw input, before anything is filtered out, and
    # before the normalisation loop below walks the whole list. Checking the
    # post-filter count instead would let a 50,000-pair request through as long as
    # all but 200 of its entries were malformed, and would pay for a full Python
    # pass over the input to work that out.
    if len(pairs) > pairs_service.BATCH_LIMIT:
        return {
            "summary": (
                f"Batch too large: {len(pairs)} pairs "
                f"(max {pairs_service.BATCH_LIMIT} per call)."
            ),
            "error": "too_many_pairs",
            "counts": {},
            "rows": [],
        }

    normalised: list[tuple[str, str, int]] = []
    malformed: list[int] = []
    for index, pair in enumerate(pairs):
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            malformed.append(index)
            continue
        a, b = str(pair[0]).strip(), str(pair[1]).strip()
        if not a or not b:
            malformed.append(index)
            continue
        normalised.append((a, b, index))

    if not normalised:
        return {
            "summary": (
                "No usable pairs supplied — each pair must be a two-element "
                "list of protein identifiers."
            ),
            "error": "no_pairs",
            "counts": {},
            "rows": [],
        }

    ids, echo, problems = _resolve_filters(
        sources, experiments, interaction_types, tissues
    )
    if problems:
        return _filter_error(problems, echo)

    f = CommonFilters.from_dict(
        {
            "show": show,
            "isoform_mode": isoform_mode,
            "min_score": min_score,
            "max_score": max_score,
            "reviewed": reviewed,
            **ids,
        }
    )

    result = pairs_result(
        rows=pairs_service.check_pairs(normalised, f), resolved_filters=echo
    )
    if malformed:
        result["malformed_pair_indexes"] = malformed
        result["summary"] += (
            f" {len(malformed)} input pair(s) were skipped as malformed."
        )
    return result


@mcp.tool()
@with_db
def get_interaction_detail(
    interaction_id: Annotated[
        int,
        Field(
            description=(
                "HIPPIE interaction id, as returned in the interaction_id field "
                "of get_interactions or check_pairs."
            )
        ),
    ],
) -> dict[str, object]:
    """Get the full evidence behind one interaction.

    Returns the PMIDs, the source databases (with a per-pair deep link where the
    source supports one), the experimental methods with their PSI-MI codes and
    quality scores, and the bait-prey detection counts. This is what to call
    before attributing a claim to HIPPIE — the score alone is not a citation.
    """
    try:
        payload = detail_service.interaction_detail_payload(interaction_id)
    except Http404:
        return {
            "summary": f"No HIPPIE interaction with id {interaction_id}.",
            "error": "not_found",
        }

    a = payload["protein_a"]["symbol"]
    b = payload["protein_b"]["symbol"]
    n_pubs = len(payload["publications"])
    payload["summary"] = (
        f"{a}-{b} interaction, score {payload['score']}, supported by "
        f"{payload['n_experiments']} experiment record(s) from "
        f"{payload['n_sources']} source(s) across {n_pubs} publication(s)."
    )
    payload["detail_url"] = absolute_url(payload["detail_url"])
    return payload


@mcp.tool()
@with_db
def list_filter_options(
    kind: Annotated[
        Literal["source", "experiment", "interaction_type", "tissue"] | None,
        Field(
            default=None,
            description=(
                "Which vocabulary to list. Omit for an overview of all four "
                "with their category labels and sizes."
            ),
        ),
    ] = None,
    query: Annotated[
        str | None,
        Field(
            default=None,
            description="Case-insensitive substring to filter option names by.",
        ),
    ] = None,
) -> dict[str, object]:
    """List the vocabularies the filter arguments accept.

    Called without arguments it returns an overview: the four vocabularies, how
    many options each has, and their category labels — enough to filter by
    category without pulling several hundred individual terms into context. Pass
    ``kind`` (and optionally ``query``) to drill into one.

    Every value listed here — an option name, a PSI-MI code, a category label,
    or an id — is accepted by the filter arguments of the other tools.
    """
    if kind is None:
        overview = {}
        for k in filter_lookup.KINDS:
            options = filter_lookup.options_for(k)
            overview[k] = {
                "n_options": len(options),
                # Populated categories only. A declared-but-empty label is not a
                # usable filter value, so listing it would be an invitation to a
                # call that fails.
                "categories": filter_lookup.populated_category_order_for(
                    k, options=options
                ),
            }
        return {
            "summary": (
                "Four filter vocabularies. Filter by a category label to select "
                "every member at once, or call again with `kind` for the "
                "individual options."
            ),
            "vocabularies": overview,
        }

    options = filter_lookup.options_for(kind)
    if query:
        needle = query.casefold()
        options = [o for o in options if needle in o["name"].casefold()]

    grouped: dict[str, list[dict]] = {}
    for option in options:
        entry = {"id": option["id"], "name": option["name"]}
        if "count" in option:
            entry["n_interactions" if kind != "tissue" else "n_expressed_genes"] = (
                option["count"]
            )
        if option.get("psi_mi_code"):
            entry["psi_mi_code"] = option["psi_mi_code"]
        grouped.setdefault(option["category"], []).append(entry)

    # Declared order, narrowed to what these options actually carry — so
    # category_order and categories agree, and neither names an empty category.
    order = [
        label for label in filter_lookup.category_order_for(kind) if label in grouped
    ]
    categories = {label: grouped[label] for label in order}

    summary = f"{len(options)} {kind} option(s)"
    if query:
        summary += f" matching {query!r}"
    summary += (
        f" across {len(categories)} category/categories. Any name, id, "
        f"PSI-MI code, or category label here is a valid filter value."
    )

    return {
        "summary": summary,
        "kind": kind,
        "n_options": len(options),
        "category_order": order,
        "categories": categories,
    }
