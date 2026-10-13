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


# Full name + one-line description per source, for the filter guide. Many
# source labels are acronyms or curation-team names that mean nothing to a
# visitor (bhf-ucl, mbinfo, ntnu); this spells them out. Keyed like
# SOURCE_LINKS.
SOURCE_INFO: dict[str, dict[str, str]] = {
    "intact": {
        "full_name": "IntAct Molecular Interaction Database",
        "description": "Open, literature-curated molecular interaction database "
        "hosted at EMBL-EBI; the shared curation platform of the IMEx consortium.",
    },
    "biogrid": {
        "full_name": "Biological General Repository for Interaction Datasets",
        "description": "Literature-curated protein and genetic interactions "
        "across major model organisms and human.",
    },
    "mint": {
        "full_name": "Molecular INTeraction database",
        "description": "Literature-curated protein interactions from the "
        "University of Rome Tor Vergata; curated within IntAct (IMEx member).",
    },
    "homomint": {
        "full_name": "HomoMINT",
        "description": "Human interactions inferred from orthologous "
        "interactions in model organisms, derived from MINT.",
    },
    "dip": {
        "full_name": "Database of Interacting Proteins",
        "description": "Experimentally determined protein interactions "
        "curated at UCLA (IMEx member).",
    },
    "imex": {
        "full_name": "International Molecular Exchange Consortium",
        "description": "Consortium of interaction databases (IntAct, MINT, DIP, "
        "…) sharing one curation standard; records curated to IMEx rules.",
    },
    "mpidb": {
        "full_name": "Microbial Protein Interaction Database",
        "description": "Curated microbial protein interactions; the original "
        "site is offline and its data lives on in IntAct.",
    },
    "matrixdb": {
        "full_name": "MatrixDB",
        "description": "Interactions involving extracellular-matrix proteins, "
        "proteoglycans and polysaccharides (University of Lyon).",
    },
    "innatedb": {
        "full_name": "InnateDB",
        "description": "Curated interactions and pathways of the innate immune "
        "response.",
    },
    "hpidb": {
        "full_name": "Host–Pathogen Interaction Database",
        "description": "Curated interactions between host and pathogen proteins "
        "(Mississippi State University).",
    },
    "uniprot": {
        "full_name": "UniProt Knowledgebase",
        "description": "Interactions annotated by UniProtKB/Swiss-Prot curators "
        "and deposited via IntAct.",
    },
    "bhf-ucl": {
        "full_name": "British Heart Foundation – University College London",
        "description": "Cardiovascular gene-annotation team at UCL that curates "
        "interactions of cardiovascular-relevant proteins into IntAct.",
    },
    "i2d": {
        "full_name": "Interologous Interaction Database",
        "description": "Known and predicted protein interactions collected by "
        "the Jurisica lab, University of Toronto.",
    },
    "hprd": {
        "full_name": "Human Protein Reference Database",
        "description": "Manually curated human protein interactions; no longer "
        "updated.",
    },
    "molecular connections": {
        "full_name": "Molecular Connections",
        "description": "Commercial curation team (Bangalore, India) that curates "
        "interaction data into IntAct.",
    },
    "mbinfo": {
        "full_name": "MechanoBiology Institute (MBInfo)",
        "description": "Curation team of the MechanoBiology Institute, National "
        "University of Singapore, contributing mechanobiology-related "
        "interactions to IntAct.",
    },
    "ntnu": {
        "full_name": "Norwegian University of Science and Technology",
        "description": "Curation team at NTNU (Trondheim) contributing "
        "interactions to IntAct.",
    },
    "flybase": {
        "full_name": "FlyBase",
        "description": "Drosophila genetics and genomics database; contributes "
        "curated interaction records via IntAct.",
    },
    "interpro": {
        "full_name": "InterPro",
        "description": "Protein families, domains and functional sites (EMBL-EBI).",
    },
    "omim": {
        "full_name": "Online Mendelian Inheritance in Man",
        "description": "Catalogue of human genes and genetic disorders.",
    },
    "go": {
        "full_name": "Gene Ontology",
        "description": "Gene-function annotation resource; interaction records "
        "originating from GO curation.",
    },
    "brenda": {
        "full_name": "BRENDA",
        "description": "Comprehensive enzyme information system.",
    },
    "efo": {
        "full_name": "Experimental Factor Ontology",
        "description": "EMBL-EBI ontology of experimental variables.",
    },
    "tissue list": {
        "full_name": "UniProt tissue list",
        "description": "UniProt's controlled vocabulary of tissues.",
    },
    "pmc": {
        "full_name": "PubMed Central",
        "description": "Free full-text archive of biomedical literature.",
    },
    "pdbe": {
        "full_name": "Protein Data Bank in Europe",
        "description": "European archive of macromolecular structures.",
    },
    "rcsb pdb": {
        "full_name": "RCSB Protein Data Bank",
        "description": "US archive of macromolecular structures.",
    },
    "wwpdb": {
        "full_name": "Worldwide Protein Data Bank",
        "description": "Consortium maintaining the single global PDB archive.",
    },
    "emdb": {
        "full_name": "Electron Microscopy Data Bank",
        "description": "Archive of cryo-EM density maps.",
    },
    "empiar": {
        "full_name": "Electron Microscopy Public Image Archive",
        "description": "Archive of raw electron-microscopy image data.",
    },
    "bmrb": {
        "full_name": "Biological Magnetic Resonance Data Bank",
        "description": "Archive of NMR spectroscopy data on biomolecules.",
    },
    "pride": {
        "full_name": "PRIDE",
        "description": "Proteomics identifications database (mass-spectrometry "
        "data, EMBL-EBI).",
    },
    "proteomexchange": {
        "full_name": "ProteomeXchange",
        "description": "Consortium coordinating public mass-spectrometry "
        "proteomics repositories.",
    },
}


def source_info(name: str) -> dict[str, str]:
    """Full name + description for a source name, or empty strings if unknown."""
    info = SOURCE_INFO.get(_key(name), {})
    return {
        "full_name": info.get("full_name", ""),
        "description": info.get("description", ""),
    }


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
