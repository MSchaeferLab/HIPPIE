"""DIGGER (exbio.wzw.tum.de/digger) cross-link builders for the detail pages.

Single source of truth, used by ``views._digger_ctx`` to render the
"Further information" card on the interaction / non-interaction detail pages.

DIGGER is keyed by Ensembl IDs:
  - canonical protein → ENSG (gene):   ``…/digger/ID/gene/human/{ensg}``
  - isoform          → ENST/ENSP:      ``…/digger/ID/human/{enst|ensp}``
  - isoform pair     → accessions:     ``…/digger/ID/gene/human/multiple/{a},{b}``
  - canonical pair   → POST network_analysis with both ENSGs (a plain <a> can't
    POST, so the template renders a small form; here we only build the payload).

``ensg`` / ``enst`` / ``ensp`` arrive as lists of unversioned IDs (see
``Gene.ensg`` / ``Isoform.enst`` / ``Isoform.ensp``). We use the first entry of
each list and apply a fallback chain; an empty list means "not available".
"""

from __future__ import annotations

BASE = "https://exbio.wzw.tum.de/digger"


def _first(ids: list[str]) -> str | None:
    """First non-empty entry of an ID list, or None."""
    for id_ in ids:
        if id_:
            return id_
    return None


def protein_digger_url(
    *,
    is_isoform: bool,
    ensg: list[str],
    enst: list[str],
    ensp: list[str],
) -> str | None:
    """DIGGER link for a single protein, or None when nothing resolves.

    Canonical: gene ENSG only.
    Isoform:   ENST, else ENSP, else the connected gene's ENSG (``ensg``).
    """
    if is_isoform:
        transcript = _first(enst)
        if transcript:
            return f"{BASE}/ID/human/{transcript}"
        translation = _first(ensp)
        if translation:
            return f"{BASE}/ID/human/{translation}"
        gene = _first(ensg)
        if gene:
            return f"{BASE}/ID/gene/human/{gene}"
        return None

    gene = _first(ensg)
    if gene:
        return f"{BASE}/ID/gene/human/{gene}"
    return None


def interaction_digger(
    *,
    p1_is_isoform: bool,
    p2_is_isoform: bool,
    p1_enst_p: str,
    p2_enst_p: str,
    g1_ensg: list[str],
    g2_ensg: list[str],
) -> dict[str, str]:
    """DIGGER link for the pair.

    Returns a dict describing how the template should render it:
      - both isoforms   → {"kind": "link", "url": …}  (uses UniProt accessions)
      - both canonical  → {"kind": "form", "action": …, "post_input": "E1\\r\\nE2"}
                          when both genes resolve, else {"kind": "none"}
      - mixed           → {"kind": "none"}
    """
    if p1_is_isoform and p2_is_isoform:
        return {
            "kind": "link",
            "url": f"{BASE}/ID/gene/human/multiple/{p1_enst_p},{p2_enst_p}",
        }
    if not p1_is_isoform and not p2_is_isoform:
        ensg1 = _first(g1_ensg)
        ensg2 = _first(g2_ensg)
        if ensg1 and ensg2:
            return {
                "kind": "form",
                "action": f"{BASE}/network_analysis/",
                # CRLF-joined; the browser encodes it as %0D%0A in the POST body,
                # matching DIGGER's expected ``input=E1%0D%0AE2`` payload.
                "post_input": f"{ensg1}\r\n{ensg2}",
            }
        return {"kind": "none"}
    # one isoform, one canonical — DIGGER has no combined view.
    return {"kind": "none"}
