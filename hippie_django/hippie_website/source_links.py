"""Homepage URL, description and per-pair-search URLs for source databases.

Homepage / descriptions live in ``data/sources_db.json`` (edit that file, then run
``manage.py update_sources`` to write it into the ``Source`` table; ``hippie_update``
does this automatically). This module reads that file and owns the code-side part:
the IntAct pair-search URL templates.

Used by:
  - management/commands/update_sources.py → ``Source.url`` / ``Source.description``
  - services/detail.py                    (``pair_search_url``) → per-pair link
  - services/guide.py                     (``source_info``)     → filter guide
  - management/commands/export_downloads.py (``psi_mi_code``)   → MITAB column 13

Keys are lowercased source names; matching is case-insensitive because MITAB
emits lowercase tokens ("intact", "biogrid") while seed data uses display
casing ("IntAct", "BioGRID").

Per-pair links: IntAct's pairwise search lists every IMEx/IntAct evidence for a
protein pair. Every IMEx-member database deposits its curated data into IntAct,
so the pair evidence contributed by MINT, DIP, MatrixDB, InnateDB, HPIDB,
UniProt and bhf-ucl is viewable there too. Each link is scoped to its source
database via the MIQL ``source:`` field (e.g. ``id:A AND id:B AND source:dip``)
so it shows only that source's evidence for the pair. (IMEx is the consortium
dataset, not a single source value, so its link is left unscoped.) Databases
without a key-free pairwise web URL (BioGRID, I2D, HPRD) and annotation /
structure resources are homepage-only.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from .management.commands._sources import DATA_DIR


def _intact_pair(source: str | None = None) -> str:
    """IntAct pairwise-search URL template ({a}/{b} = the two UniProt accs).

    When ``source`` is given, the MIQL query is scoped to that source database
    (``AND source:<db>``) so the link surfaces only that source's evidence for
    the pair. Tokens containing '-' are quoted because MIQL treats a bare '-' as
    a NOT operator. See the ``intact-miql-fields`` memory for the field list.
    """
    query = "id:{a}%20AND%20id:{b}"
    if source:
        token = f"%22{source}%22" if "-" in source else source
        query += f"%20AND%20source:{token}"
    return "https://www.ebi.ac.uk/intact/search?query=" + query


# Sources whose pairwise evidence is viewable in IntAct, scoped by ``source:``.
# ``imex`` is the consortium, not a source value, so its link is unscoped.
_INTACT_PAIR_SOURCES: frozenset[str] = frozenset(
    {
        "intact",
        "mint",
        "mpidb",
        "dip",
        "matrixdb",
        "innatedb",
        "hpidb",
        "uniprot",
        "bhf-ucl",
    }
)

# Casing/spacing variants seen in data → canonical registry key.
_ALIASES: dict[str, str] = {
    "rcsb-pdb": "rcsb pdb",
    "rcsbpdb": "rcsb pdb",
    "pdb": "rcsb pdb",
    "molecularconnections": "molecular connections",
    "tisslist": "tissue list",
    "gene ontology": "go",
    "mpid": "mpidb",
}


def _key(name: str) -> str:
    k = name.strip().lower()
    return _ALIASES.get(k, k)


def registry_path() -> Path:
    """``$HIPPIE_SOURCES_DB`` if set, else ``data/sources_db.json``."""
    override = os.environ.get("HIPPIE_SOURCES_DB")
    return Path(override) if override else DATA_DIR / "sources_db.json"


@lru_cache(maxsize=4)
def _read_registry(path: Path, mtime_ns: int) -> dict[str, dict[str, str]]:
    with path.open(encoding="utf-8") as f:
        data: dict[str, dict[str, str]] = json.load(f)
    return {k.strip().lower(): v for k, v in data.items()}


def load_registry(path: Path | None = None) -> dict[str, dict[str, str]]:
    """Registry ``{key: {"url", "description", "what_it_is", "psi_mi"}}`` read from JSON.

    Cached per (path, mtime), so edits to the file are picked up without a restart.
    """
    p = path or registry_path()
    return _read_registry(p, p.stat().st_mtime_ns)


def registry_entry(name: str) -> dict[str, str] | None:
    """Registry entry for a source name (alias/case-insensitive), or None."""
    return load_registry().get(_key(name))


def source_info(name: str) -> dict[str, str]:
    """Short description + longer explanation for a source, "" when unknown."""
    info = registry_entry(name) or {}
    return {
        "full_name": info.get("description", ""),
        "description": info.get("what_it_is", ""),
    }


def homepage_url(name: str) -> str:
    """Return the known homepage URL for a source name, or "" if unknown."""
    return (registry_entry(name) or {}).get("url", "")


def psi_mi_code(name: str) -> str:
    """PSI-MI source-database term (e.g. "MI:0463"), or "" if the registry has none.

    The MITAB export writes it into the Source Databases column, which PSI-MI
    TAB 2.5 requires to be a controlled-vocabulary term.
    """
    return (registry_entry(name) or {}).get("psi_mi", "")


def pair_search_url(name: str, acc_a: str | None, acc_b: str | None) -> str | None:
    """URL listing all evidence for the (acc_a, acc_b) UniProt pair in ``name``.

    Returns None when the source has no pair-search link, or when either
    accession is missing.
    """
    if not acc_a or not acc_b:
        return None
    key = _key(name)
    if key == "imex":
        template = _intact_pair()
    elif key in _INTACT_PAIR_SOURCES:
        template = _intact_pair(key)
    else:
        return None
    return template.format(a=quote(acc_a), b=quote(acc_b))
