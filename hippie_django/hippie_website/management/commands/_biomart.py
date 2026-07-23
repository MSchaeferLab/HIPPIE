"""
_biomart — bulk UniProt → Ensembl ID mapping via the Ensembl BioMart service.

``hippie_update`` fills ``Gene.ensg`` / ``Isoform.enst`` / ``Isoform.ensp``. After
the local UniProt idmapping pass, the still-empty rows used to be resolved with one
HTTP request *per isoform* against ``rest.ensembl.org`` — slow and prone to
``RemoteDisconnected`` once a few hundred requests pile up. This module replaces
that inner loop with a handful of bulk BioMart queries: for the set of gap UniProt
accessions it fetches the whole ``uniprot_isoform ↔ Ensembl`` map in ~200-accession
chunks, so thousands of per-isoform round-trips collapse into a few requests.

Each BioMart row is::

    <uniprot accession>  <ENSG>  <ENST>  <ENSP>  <uniprot_isoform>

where ``uniprot_isoform`` is the exact isoform accession (e.g. ``P04637-9``,
``P31946-1``). The caller matches its gap isoforms against that column and its gap
genes against the plain-accession → ENSG map.

Design: pure parsing (:func:`parse_biomart_tsv`) is separated from I/O
(:func:`fetch_uniprot_ensembl_map`) so the parser is unit-testable without network
access, and this module is self-contained (no import back into ``hippie_update``,
which would be circular).
"""

from __future__ import annotations

import http.client
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

BIOMART_URL = "https://www.ensembl.org/biomart/martservice"

_DATASET = "hsapiens_gene_ensembl"
_TIMEOUT = 120  # seconds; BioMart can be slow to first byte
_RETRIES = 4
_BACKOFF_CAP = 30.0
_CHUNK = 200  # accessions per query — keeps the POST body and response modest
_HEADERS = {"User-Agent": "hippie-ensembl/1.0"}

# BioMart exposes two distinct UniProt accession attributes: reviewed (SwissProt)
# and unreviewed (TrEMBL). A gap accession may sit in either, so we try SwissProt
# first and fall back to TrEMBL only for accessions still unresolved.
_UNIPROT_FILTERS = ("uniprotswissprot", "uniprotsptrembl")

# Parsed maps: isoform accession -> {"enst": [...], "ensp": [...]}, and plain
# accession -> [ENSG, ...]. Neither is deduped here; the caller applies its own
# order-preserving de-dupe.
IsoMap = dict[str, dict[str, list[str]]]
AccEnsgMap = dict[str, list[str]]


def _strip_version(ensembl_id: str) -> str:
    """Drop the ``.N`` version suffix (ENSG00000141510.14 -> ENSG00000141510)."""
    return ensembl_id.split(".", 1)[0]


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def build_query_xml(uniprot_filter: str, values: list[str]) -> str:
    """BioMart XML mapping the given UniProt accessions to Ensembl IDs.

    ``uniprot_filter`` is one of ``uniprotswissprot`` / ``uniprotsptrembl``; it is
    used both as the value filter and as the first output column so the caller can
    tell which accessions were resolved.
    """
    joined = ",".join(values)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<!DOCTYPE Query>"
        '<Query virtualSchemaName="default" formatter="TSV" header="0" '
        'uniqueRows="1" count="" datasetConfigVersion="0.6">'
        f'<Dataset name="{_DATASET}" interface="default">'
        f'<Filter name="{uniprot_filter}" value="{joined}"/>'
        f'<Attribute name="{uniprot_filter}"/>'
        '<Attribute name="ensembl_gene_id"/>'
        '<Attribute name="ensembl_transcript_id"/>'
        '<Attribute name="ensembl_peptide_id"/>'
        '<Attribute name="uniprot_isoform"/>'
        "</Dataset></Query>"
    )


def parse_biomart_tsv(text: str) -> tuple[IsoMap, AccEnsgMap]:
    """Parse a BioMart TSV response into the two lookup maps.

    Rows with an empty ``uniprot_isoform`` still contribute their ENSG to the
    accession → ENSG map (useful for gene gaps) but add nothing to the isoform map.
    Ensembl IDs are version-stripped defensively (BioMart usually returns them
    unversioned already).
    """
    iso_map: IsoMap = defaultdict(lambda: {"enst": [], "ensp": []})
    acc_ensg: AccEnsgMap = defaultdict(list)
    for line in text.splitlines():
        if not line.strip():
            continue
        cols = line.split("\t")
        if len(cols) < 5:
            continue
        acc, ensg, enst, ensp, iso = (c.strip() for c in cols[:5])
        if acc and ensg:
            acc_ensg[acc].append(_strip_version(ensg))
        if iso:
            if enst:
                iso_map[iso]["enst"].append(_strip_version(enst))
            if ensp:
                iso_map[iso]["ensp"].append(_strip_version(ensp))
    return dict(iso_map), dict(acc_ensg)


def _post_query(xml: str) -> str:
    """POST one BioMart query, retrying transient failures. Raises on final failure."""
    body = urllib.parse.urlencode({"query": xml}).encode()
    last_exc: Exception | None = None
    for attempt in range(_RETRIES):
        try:
            req = urllib.request.Request(BIOMART_URL, data=body, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                text = resp.read().decode("utf-8", "replace")
            # BioMart reports query problems as a plain-text body, HTTP 200.
            if text.lstrip().startswith("Query ERROR"):
                raise ValueError(text.strip()[:200])
            return text
        except (
            urllib.error.URLError,
            http.client.HTTPException,
            ConnectionError,
            TimeoutError,
            OSError,
            ValueError,
        ) as e:
            last_exc = e
            time.sleep(min(_BACKOFF_CAP, 1.0 * (2**attempt)) + random.uniform(0, 0.5))
    raise RuntimeError(f"BioMart query failed after {_RETRIES} attempts: {last_exc}")


def fetch_uniprot_ensembl_map(
    accessions: set[str], *, stdout: object | None = None
) -> tuple[IsoMap, AccEnsgMap]:
    """Bulk-resolve ``accessions`` (plain UniProt accessions) to Ensembl IDs.

    Queries SwissProt first, then TrEMBL for whatever is still unresolved, in
    chunks of :data:`_CHUNK`. Returns the merged ``(iso_map, acc_ensg)`` maps.
    Raises ``RuntimeError`` if a query ultimately fails — the caller treats the
    whole enrichment step as non-fatal.
    """
    iso_map: IsoMap = defaultdict(lambda: {"enst": [], "ensp": []})
    acc_ensg: AccEnsgMap = defaultdict(list)
    remaining = {a for a in accessions if a}
    if not remaining:
        return {}, {}

    for uniprot_filter in _UNIPROT_FILTERS:
        if not remaining:
            break
        found: set[str] = set()
        chunks = _chunks(sorted(remaining), _CHUNK)
        if stdout is not None:
            stdout.write(
                f"  BioMart: {len(remaining)} accession(s) via {uniprot_filter} "
                f"({len(chunks)} chunk(s))..."
            )
        for chunk in chunks:
            text = _post_query(build_query_xml(uniprot_filter, chunk))
            chunk_iso, chunk_ensg = parse_biomart_tsv(text)
            for iso, ids in chunk_iso.items():
                iso_map[iso]["enst"].extend(ids["enst"])
                iso_map[iso]["ensp"].extend(ids["ensp"])
            for acc, ensgs in chunk_ensg.items():
                acc_ensg[acc].extend(ensgs)
                found.add(acc)
        remaining -= found

    return dict(iso_map), dict(acc_ensg)
