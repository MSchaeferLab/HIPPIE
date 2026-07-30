"""Result shaping: caps, truncation reporting, and summary-vs-rows formatting.

Every byte a tool returns lands in the caller's context window, and HIPPIE is
big enough to fill one by accident — a hub protein like TP53 has thousands of
partners. So the default is summary-first: a readable sentence plus the top rows
by score, with the full count stated. Bulk callers opt in with a larger ``limit``
and ``format="rows"``.

Two rules the shaping here exists to enforce:

* **Never truncate silently.** A capped result always reports ``total`` next to
  ``returned`` and sets ``truncated``. A list that stops at 25 with no note reads
  as "that is all there is", which is a wrong answer, not a terse one.
* **Lead with the answer.** ``summary`` is the first key so a model reading the
  serialized result hits the conclusion before the data.
"""

import os

# Researcher-first default: small enough to read, big enough to be useful.
DEFAULT_LIMIT = 25
# Hard ceiling for a single call, matching the interaction-pair BATCH_LIMIT.
MAX_LIMIT = 200

# Public base for deep links back into the website (e.g.
# "https://cbdm-01.zdv.uni-mainz.de/~mschaefer/hippie"). Unset in dev, in which
# case links are emitted as site-relative paths.
PUBLIC_BASE_URL = os.environ.get("HIPPIE_PUBLIC_BASE_URL", "").rstrip("/")


def clamp_limit(limit: int | None) -> int:
    """Coerce a caller-supplied limit into ``1..MAX_LIMIT``."""
    if limit is None:
        return DEFAULT_LIMIT
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(MAX_LIMIT, value))


def absolute_url(path: str) -> str:
    """Turn a site-relative path into an absolute URL where the base is known."""
    if not path:
        return ""
    if not PUBLIC_BASE_URL:
        return path
    return f"{PUBLIC_BASE_URL}{path}"


def truncation_note(total: int, returned: int, *, noun: str = "results") -> str:
    """One clause stating what was withheld, or empty when nothing was."""
    if returned >= total:
        return ""
    return (
        f" Showing the top {returned} of {total} {noun} by score — "
        f"raise `limit` (max {MAX_LIMIT}) or narrow the filters for the rest."
    )


# ---------------------------------------------------------------------------
# Edge rows
# ---------------------------------------------------------------------------


def flatten_edge_row(row: dict) -> dict:
    """Flatten a ``services.queries.interaction_rows`` row for a tool result.

    The service returns nested ``query_side`` / ``partner`` protein dicts, which
    is what the React table wants. A caller reading text wants one flat record
    per edge, and does not need the internal protein PKs.
    """
    query_side = row["query_side"]
    partner = row["partner"]
    return {
        "query_protein": query_side["symbol"],
        "partner": partner["symbol"],
        "partner_uniprot": partner["uniprot_id"],
        "partner_entrez": partner["gene_id"],
        "partner_reviewed": partner["is_reviewed"],
        "score": row["score"],
        "n_sources": row["source_count"],
        "n_experiments": row["experiment_count"],
        "is_noninteraction": row["is_noninteraction"],
        "interaction_id": row["id"],
        "detail_url": absolute_url(row["detail_url"]),
    }


def _top_partners(rows: list[dict], count: int = 10) -> str:
    """Name the highest-scoring partners for the prose summary.

    HIPPIE holds several protein records per gene symbol (TP53 alone has eight),
    so two different edges can both read "MDM2". A bare symbol list would then
    show the same name twice with no way to tell the rows apart — so a symbol
    that repeats within the shown set carries its UniProt accession.
    """
    shown = rows[:count]
    seen: dict[str, int] = {}
    for row in shown:
        seen[row["partner"]] = seen.get(row["partner"], 0) + 1

    parts = []
    for row in shown:
        label = row["partner"]
        if seen[label] > 1 and row["partner_uniprot"]:
            label = f"{label} [{row['partner_uniprot']}]"
        parts.append(f"{label} ({row['score']})")
    return ", ".join(parts)


def _describe_query(proteins: list[str], unresolved: list[str], show: str) -> str:
    noun = {
        "interactions": "interactions",
        "noninteractions": "non-interactions",
        "both": "interactions and non-interactions",
    }.get(show, "interactions")
    subject = ", ".join(proteins) if proteins else "no proteins"
    text = f"{noun.capitalize()} for {subject}"
    if unresolved:
        text += f" (unmatched identifiers: {', '.join(unresolved)})"
    return text


def interactions_result(
    *,
    rows: list[dict],
    limit: int,
    query_symbols: list[str],
    unresolved: list[str],
    show: str,
    resolved_filters: dict,
    fmt: str,
    total: int | None = None,
) -> dict:
    """Assemble the ``get_interactions`` payload.

    ``rows`` is score-ordered by the service and may already be capped at
    ``limit`` — in which case ``total`` carries the true match count, because
    ``len(rows)`` no longer knows it. Pass ``total=None`` only when ``rows`` is
    the complete set.
    """
    total = len(rows) if total is None else total
    kept = [flatten_edge_row(r) for r in rows[:limit]]
    noun = "non-interactions" if show == "noninteractions" else "interactions"

    if total == 0:
        summary = (
            f"No {noun} found for {', '.join(query_symbols) or 'the query'}"
            f"{' with the active filters' if resolved_filters else ''}."
        )
        if unresolved:
            summary += f" Unmatched identifiers: {', '.join(unresolved)}."
    else:
        lead = _describe_query(query_symbols, unresolved, show)
        summary = f"{lead}: {total} match{'es' if total != 1 else ''}."
        summary += truncation_note(total, len(kept), noun=noun)
        if fmt == "summary":
            summary += f" Highest scoring: {_top_partners(kept)}."

    out: dict = {
        "summary": summary,
        "total": total,
        "returned": len(kept),
        "truncated": len(kept) < total,
        "rows": kept,
    }
    if unresolved:
        out["unresolved_identifiers"] = unresolved
    if resolved_filters:
        out["resolved_filters"] = resolved_filters
    return out


# ---------------------------------------------------------------------------
# Pair rows
# ---------------------------------------------------------------------------


def flatten_pair_row(row: dict) -> dict:
    """Flatten a ``services.pairs`` row, making the not-found cases explicit.

    A raw pair row encodes three different outcomes in ``score == -1.0``. An
    agent cannot act on that ambiguity, so it is spelled out in ``outcome``:

    * ``interacts`` / ``does_not_interact`` — a record exists
    * ``unknown_identifier`` — at least one side did not resolve
    * ``no_record`` — both sides resolved, nothing recorded (or it failed the
      active filters)
    """
    found = row["score"] >= 0
    resolved_both = bool(row["uniprot_a"]) and bool(row["uniprot_b"])
    if found:
        outcome = "does_not_interact" if row["is_noninteraction"] else "interacts"
    elif not resolved_both:
        outcome = "unknown_identifier"
    else:
        outcome = "no_record"

    return {
        "input_a": row["input_a"],
        "input_b": row["input_b"],
        "outcome": outcome,
        "symbol_a": row["symbol_a"],
        "symbol_b": row["symbol_b"],
        "uniprot_a": row["uniprot_a"] or None,
        "uniprot_b": row["uniprot_b"] or None,
        "score": row["score"] if found else None,
        "n_sources": row["source_count"] if found else None,
        "n_experiments": row["experiment_count"] if found else None,
        "interaction_id": row["interaction_id"],
        "detail_url": absolute_url(row["detail_url"]),
    }


def pairs_result(*, rows: list[dict], resolved_filters: dict) -> dict:
    """Assemble the ``check_pairs`` payload.

    Rows are capped at ``MAX_LIMIT`` — the same ceiling as the input
    ``BATCH_LIMIT``, so a well-formed batch is never truncated. Isoform mode is
    what makes the cap necessary: one input pair fans out to one row per isoform
    combination that has a record, so 200 pairs in can be more than 200 rows out.
    ``counts`` is computed over the full set, not the kept slice, so the totals
    describe the batch that was actually checked.
    """
    flat_all = [flatten_pair_row(r) for r in rows]
    total = len(flat_all)
    # Input order, not score order — check_pairs answers per input pair.
    flat = flat_all[:MAX_LIMIT]

    counts: dict[str, int] = {}
    for r in flat_all:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1

    parts = []
    if counts.get("interacts"):
        parts.append(f"{counts['interacts']} interacting")
    if counts.get("does_not_interact"):
        parts.append(f"{counts['does_not_interact']} recorded non-interacting")
    if counts.get("no_record"):
        parts.append(f"{counts['no_record']} with no record")
    if counts.get("unknown_identifier"):
        parts.append(f"{counts['unknown_identifier']} with an unknown identifier")

    summary = f"Checked {total} pair{'s' if total != 1 else ''}"
    summary += f": {', '.join(parts)}." if parts else "."
    if len(flat) < total:
        summary += (
            f" Returning the first {len(flat)} of {total} result rows "
            f"(cap {MAX_LIMIT}); the counts above cover all {total}. "
            f"Split the batch to see the rest."
        )

    out: dict = {
        "summary": summary,
        "counts": counts,
        "total": total,
        "returned": len(flat),
        "truncated": len(flat) < total,
        "rows": flat,
    }
    if resolved_filters:
        out["resolved_filters"] = resolved_filters
    return out
