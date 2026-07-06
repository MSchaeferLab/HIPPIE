"""Pure statistical helpers shared across HIPPIE (no Django / model deps)."""

from __future__ import annotations

import statistics


def compute_quartiles(scores: list[float]) -> dict[str, float]:
    """
    Summary statistics for a list of numeric scores.

    Returns keys ``q1``, ``median`` (Q2), ``q3``, ``min``, ``max``, ``mean``,
    ``std`` and ``n``. Uses the inclusive quartile method, matching the
    downloadable stats file. An empty list yields all-zero stats; a single value
    collapses the quartiles to that value with ``std`` 0.0.
    """
    n = len(scores)
    if n == 0:
        return {
            "q1": 0.0,
            "median": 0.0,
            "q3": 0.0,
            "min": 0.0,
            "max": 0.0,
            "mean": 0.0,
            "std": 0.0,
            "n": 0.0,
        }
    s = sorted(scores)
    mean = statistics.fmean(s)
    if n >= 2:
        q1, median, q3 = statistics.quantiles(s, n=4, method="inclusive")
        std = statistics.stdev(s)
    else:
        q1 = median = q3 = s[0]
        std = 0.0
    return {
        "q1": q1,
        "median": median,
        "q3": q3,
        "min": s[0],
        "max": s[-1],
        "mean": mean,
        "std": std,
        "n": float(n),
    }
