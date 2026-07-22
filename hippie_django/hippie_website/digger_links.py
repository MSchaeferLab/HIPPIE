"""DIGGER (exbio.wzw.tum.de/digger) cross-link builders for the detail pages.

Single source of truth, used by ``views._digger_ctx`` to render the
"Further information" card on the interaction / non-interaction detail pages.

DIGGER is keyed by Ensembl IDs:
  - canonical protein → ENSG (gene):   ``…/digger/ID/gene/human/{ensg}``
  - isoform          → ENST/ENSP:      ``…/digger/ID/human/{enst|ensp}``
  - isoform pair     → accessions:     ``…/digger/ID/gene/human/multiple/{a},{b}``
  - canonical pair   → signed-token GET link into ``…/digger/receive-token/``
    (see ``_handoff_url``); DIGGER verifies it with the shared secret.

``ensg`` / ``enst`` / ``ensp`` arrive as lists of unversioned IDs (see
``Gene.ensg`` / ``Isoform.enst`` / ``Isoform.ensp``). We use the first entry of
each list and apply a fallback chain; an empty list means "not available".
"""

from __future__ import annotations

from django.core import signing

BASE = "https://exbio.wzw.tum.de/digger"

# Salt for the DIGGER signed-token handoff; must match DIGGER's ``salt`` exactly.
HANDOFF_SALT = "hippie-handoff"


def _handoff_url(input_ids: list[str], secret: str) -> str:
    """Signed-token GET link into DIGGER's ``receive-token`` endpoint.

    The token carries ``{"organism": "human", "input": [...]}`` signed with the
    shared ``secret`` (default JSON serializer, ``HANDOFF_SALT``). DIGGER's
    ``Multi_proteins`` dispatches on the first id: ENSG → gene analysis,
    ENST/ENSP → isoform analysis.
    """
    token = signing.dumps(
        {"organism": "human", "input": input_ids},
        key=secret,
        salt=HANDOFF_SALT,
    )
    return f"{BASE}/receive-token/?token={token}"


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
    handoff_secret: str,
) -> dict[str, str]:
    """DIGGER link for the pair.

    Returns a dict describing how the template should render it:
      - both isoforms   → {"kind": "link", "url": …}  (uses UniProt accessions)
      - both canonical  → {"kind": "link", "url": …}  (signed-token handoff on
                          both ENSGs) when both genes resolve, else {"kind": "none"}
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
                "kind": "link",
                "url": _handoff_url([ensg1, ensg2], handoff_secret),
            }
        return {"kind": "none"}
    # one isoform, one canonical — DIGGER has no combined view.
    return {"kind": "none"}
