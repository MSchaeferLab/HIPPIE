"""Canonical homepage + per-pair-search URLs for interaction source databases.

Single source of truth, used by:
  - management/commands/hippie_update.py  (``_assign_source_urls``)  → ``Source.url``
  - views.interaction_detail_view          (``pair_search_url``)      → per-pair link

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

from urllib.parse import quote


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


SOURCE_LINKS: dict[str, dict[str, str]] = {
    # — PPI databases: IntAct pairwise search scoped to that source database —
    "intact": {"home": "https://www.ebi.ac.uk/intact/", "pair": _intact_pair("intact")},
    "mint": {"home": "https://mint.bio.uniroma2.it/", "pair": _intact_pair("mint")},
    "imex": {"home": "https://www.imexconsortium.org/", "pair": _intact_pair()},
    "mpidb": {
        "home": "",
        "pair": _intact_pair("mpidb"),
    },  # own site defunct; data in IntAct
    "dip": {"home": "https://dip.doe-mbi.ucla.edu/", "pair": _intact_pair("dip")},
    "matrixdb": {
        "home": "https://matrixdb.univ-lyon1.fr/",
        "pair": _intact_pair("matrixdb"),
    },
    "innatedb": {"home": "https://www.innatedb.com/", "pair": _intact_pair("innatedb")},
    "hpidb": {"home": "https://hpidb.igbb.msstate.edu/", "pair": _intact_pair("hpidb")},
    "uniprot": {"home": "https://www.uniprot.org/", "pair": _intact_pair("uniprot")},
    "bhf-ucl": {
        "home": "https://www.ebi.ac.uk/GOA/CVI",
        "pair": _intact_pair("bhf-ucl"),
    },
    # — PPI databases without a key-free pairwise web URL → homepage only —
    "biogrid": {"home": "https://thebiogrid.org/"},
    "i2d": {"home": "http://ophid.utoronto.ca/"},
    "homomint": {"home": "https://mint.bio.uniroma2.it/"},
    "hprd": {"home": "http://www.hprd.org/"},
    # — Annotation / structure / literature databases → homepage only —
    "interpro": {"home": "https://www.ebi.ac.uk/interpro/"},
    "omim": {"home": "https://www.omim.org/"},
    "molecular connections": {"home": "https://www.molecularconnections.com/"},
    "emdb": {"home": "https://www.ebi.ac.uk/emdb/"},
    "proteomexchange": {"home": "https://www.proteomexchange.org/"},
    "pmc": {"home": "https://www.ncbi.nlm.nih.gov/pmc/"},
    "brenda": {"home": "https://www.brenda-enzymes.org/"},
    "efo": {"home": "https://www.ebi.ac.uk/efo/"},
    "flybase": {"home": "https://flybase.org/"},
    "empiar": {"home": "https://www.ebi.ac.uk/empiar/"},
    "rcsb pdb": {"home": "https://www.rcsb.org/"},
    "go": {"home": "https://geneontology.org/"},
    "pride": {"home": "https://www.ebi.ac.uk/pride/"},
    "wwpdb": {"home": "https://www.wwpdb.org/"},
    "mbinfo": {"home": "https://www.mechanobio.info/"},
    "tissue list": {"home": "https://www.uniprot.org/help/tissue_list"},
    "ntnu": {"home": "https://www.ntnu.edu/"},
    "bmrb": {"home": "https://bmrb.io/"},
    "pdbe": {"home": "https://www.ebi.ac.uk/pdbe/"},
}

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


def homepage_url(name: str) -> str:
    """Return the known homepage URL for a source name, or "" if unknown."""
    return SOURCE_LINKS.get(_key(name), {}).get("home", "")


def pair_search_url(name: str, acc_a: str | None, acc_b: str | None) -> str | None:
    """URL listing all evidence for the (acc_a, acc_b) UniProt pair in ``name``.

    Returns None when the source has no pair-search link, or when either
    accession is missing.
    """
    if not acc_a or not acc_b:
        return None
    template = SOURCE_LINKS.get(_key(name), {}).get("pair")
    if not template:
        return None
    return template.format(a=quote(acc_a), b=quote(acc_b))
