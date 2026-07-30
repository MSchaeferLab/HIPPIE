"""Curated category buckets for the three evidence vocabularies.

Single source of truth, used by ``views._filter_option_lists`` to attach a
``category`` to every filter option. The frontend renders one collapsible group
per category, so a user can tick "Two-hybrid & complementation" instead of
hunting through 190 individual experiment types.

Why a hand-curated registry rather than the PSI-MI ontology: the ontology's own
hierarchy is deeper and narrower than what a filter UI wants (``MI:0018`` two
hybrid sits four levels under ``MI:0045``), and importing it would add an
external data dependency and an import step for a mapping that changes maybe
once a year. This module follows the ``source_links.py`` precedent — an in-repo
dict, reviewed in PRs, with a test asserting every vocabulary row is covered.

Experiment types are keyed by PSI-MI code because their display names are
rewritten by the MITAB importer; interaction types and sources are keyed by
lowercased name because they carry no code (see
``hippie_update.py`` — InteractionType rows are created with ``psi_mi_code=""``).

The ``*_CATEGORY_ORDER`` tuples are alphabetical by display name — the frontend
sorts categories, sub-categories and options by name too, so a user hunting for
a term they already know can scan for it. Anything unmapped falls into
:data:`OTHER`, which is pinned last everywhere. A new vocabulary row therefore
degrades to "Other" rather than disappearing from the UI.
"""

from __future__ import annotations

OTHER = "Other"

# ── Experiment types (keyed by PSI-MI code) ─────────────────────────────────

TWO_HYBRID = "Two-hybrid & complementation"
AFFINITY = "Affinity purification & pull-down"
PROXIMITY = "Proximity labelling & cross-linking"
MASS_SPEC = "Mass spectrometry"
FRACTIONATION = "Co-fractionation & co-migration"
MICROSCOPY = "Microscopy & imaging"
FLUORESCENCE = "Fluorescence & luminescence readouts"
STRUCTURAL = "Structural biology"
BINDING = "Binding & biophysical assays"
AGGREGATION = "Aggregation & self-assembly"
ARRAYS = "Arrays & display technologies"
ENZYMATIC = "Enzymatic & functional assays"
CLEAVAGE_ASSAYS = "Cleavage & protease assays"

# Display order for the experiment-type groups: alphabetical, OTHER last.
EXPERIMENT_CATEGORY_ORDER: tuple[str, ...] = (
    AFFINITY,
    AGGREGATION,
    ARRAYS,
    BINDING,
    CLEAVAGE_ASSAYS,
    FRACTIONATION,
    ENZYMATIC,
    FLUORESCENCE,
    MASS_SPEC,
    MICROSCOPY,
    PROXIMITY,
    STRUCTURAL,
    TWO_HYBRID,
    OTHER,
)

_EXPERIMENT_CODES: dict[str, tuple[str, ...]] = {
    TWO_HYBRID: (
        "MI:0018",  # two hybrid
        "MI:0397",  # two hybrid array
        "MI:0398",  # two hybrid pooling approach
        "MI:0399",  # two hybrid fragment pooling approach
        "MI:1113",  # two hybrid bait and prey pooling approach
        "MI:1356",  # validated two hybrid
        "MI:2215",  # barcode fusion genetics two hybrid
        "MI:0655",  # lambda repressor two hybrid
        "MI:0726",  # reverse two hybrid
        "MI:1320",  # membrane yeast two hybrid
        "MI:2413",  # mammalian membrane two hybrid
        "MI:0588",  # three hybrid
        "MI:0437",  # protein three hybrid
        "MI:0231",  # mammalian protein protein interaction trap
        "MI:0097",  # reverse ras recruitment system
        "MI:0090",  # protein complementation assay
        "MI:0809",  # bimolecular fluorescence complementation
        "MI:0010",  # beta galactosidase complementation
        "MI:0011",  # beta lactamase complementation
        "MI:0728",  # gal4 vp16 complementation
        "MI:0727",  # lexa b52 complementation
        "MI:0916",  # lexa vp16 complementation
        "MI:0369",  # lex-a dimerization assay
        "MI:0370",  # tox-r dimerization assay
        "MI:0232",  # transcriptional complementation assay
        "MI:1203",  # split luciferase complementation
        "MI:1204",  # split firefly luciferase complementation
        "MI:1037",  # split renilla luciferase complementation
        "MI:0111",  # dihydrofolate reductase reconstruction
        "MI:0112",  # ubiquitin reconstruction
    ),
    AFFINITY: (
        "MI:0004",  # affinity chromatography technology
        "MI:0400",  # affinity technology
        "MI:0006",  # anti bait coimmunoprecipitation
        "MI:0007",  # anti tag coimmunoprecipitation
        "MI:0019",  # coimmunoprecipitation
        "MI:0858",  # immunodepleted coimmunoprecipitation
        # A co-IP with a luciferase readout, not a complementation assay; the
        # ontology parent is MI:0004 affinity chromatography technology.
        "MI:0729",  # luminescence based mammalian interactome mapping
        "MI:0096",  # pull down
        "MI:0676",  # tandem affinity purification
        "MI:0963",  # interactome parallel affinity capture
        "MI:0402",  # chromatin immunoprecipitation assay
        "MI:0225",  # chromatin immunoprecipitation array
        "MI:1017",  # rna immunoprecipitation
        "MI:2289",  # virotrap
        "MI:2437",  # holdup assay
    ),
    # Spatial proximity in situ only. The homogeneous bead/plate assays whose
    # names say "proximity" (MI:0905, MI:0099, MI:0425) are in vitro binding or
    # activity assays and live in BINDING / ENZYMATIC instead.
    PROXIMITY: (
        "MI:1313",  # proximity labelling technology
        "MI:1314",  # proximity-dependent biotin identification
        "MI:0813",  # proximity ligation assay
        "MI:0030",  # cross-linking study
        "MI:0031",  # protein cross-linking with a bifunctional reagent
    ),
    MASS_SPEC: (
        "MI:0943",  # detection by mass spectrometry
        "MI:0069",  # mass spectrometry studies of complexes
        "MI:0944",  # mass spectrometry study of hydrogen/deuterium exchange
        "MI:1246",  # ion mobility mass spectrometry of complexes
        "MI:0095",  # proteinchip(r) SELDI
    ),
    FRACTIONATION: (
        "MI:0071",  # molecular sieving
        "MI:0027",  # cosedimentation
        "MI:0028",  # cosedimentation in solution
        "MI:0029",  # cosedimentation through density gradient
        "MI:0807",  # comigration in gel electrophoresis
        "MI:0404",  # comigration in non denaturing gel electrophoresis
        "MI:0808",  # comigration in sds page
        "MI:0276",  # blue native page
        "MI:0226",  # ion exchange chromatography
        "MI:0227",  # reverse phase chromatography
        "MI:1022",  # field flow fractionation
    ),
    MICROSCOPY: (
        "MI:0428",  # imaging technique
        "MI:0416",  # fluorescence microscopy
        "MI:0663",  # confocal microscopy
        "MI:0426",  # light microscopy
        "MI:0872",  # atomic force microscopy
    ),
    FLUORESCENCE: (
        "MI:0055",  # fluorescent resonance energy transfer
        "MI:0012",  # bioluminescence resonance energy transfer
        "MI:2171",  # complemented donor-acceptor resonance energy transfer
        "MI:1016",  # fluorescence recovery after photobleaching
        "MI:0052",  # fluorescence correlation spectroscopy
        "MI:0053",  # fluorescence polarization spectroscopy
        "MI:0017",  # classical fluorescence spectroscopy
        "MI:0051",  # fluorescence technology
        "MI:0510",  # homogeneous time resolved fluorescence
        "MI:0976",  # total internal reflection fluorescence spectroscopy
        "MI:0054",  # fluorescence-activated cell sorting
        "MI:2169",  # luminiscence technology
    ),
    STRUCTURAL: (
        "MI:0114",  # x-ray crystallography
        "MI:0825",  # x-ray fiber diffraction
        "MI:0826",  # x ray scattering
        "MI:0894",  # electron diffraction
        "MI:0077",  # nuclear magnetic resonance
        "MI:1103",  # solution state nmr
        "MI:1104",  # solid state nmr
        "MI:0040",  # electron microscopy
        "MI:0410",  # 3D electron microscopy
        "MI:0020",  # transmission electron microscopy
        "MI:1024",  # scanning electron microscopy
        "MI:2338",  # electron tomography
        "MI:2339",  # electron microscopy 3D single particle reconstruction
        "MI:2340",  # electron microscopy 3D helical reconstruction
        "MI:0888",  # small angle neutron scattering
        "MI:0042",  # electron paramagnetic resonance
        "MI:0016",  # circular dichroism
    ),
    BINDING: (
        "MI:0107",  # surface plasmon resonance
        "MI:0969",  # bio-layer interferometry
        "MI:0065",  # isothermal titration calorimetry
        "MI:1311",  # differential scanning calorimetry
        "MI:1247",  # microscale thermophoresis
        "MI:1235",  # thermal shift binding
        "MI:0038",  # dynamic light scattering
        "MI:0104",  # static light scattering
        "MI:0067",  # light scattering
        "MI:0968",  # biosensor
        "MI:0859",  # force spectroscopy
        "MI:1038",  # silicon nanowire field-effect transistor
        "MI:0964",  # infrared spectroscopy
        "MI:0966",  # ultraviolet-visible spectroscopy
        "MI:1086",  # equilibrium dialysis
        "MI:0049",  # filter binding
        "MI:0405",  # competition binding
        "MI:0440",  # saturation binding
        "MI:0892",  # solid phase assay
        "MI:0411",  # enzyme linked immunosorbent assay
        "MI:0695",  # sandwich immunoassay
        "MI:2189",  # avexis
        # "Proximity" here describes the readout physics (singlet oxygen,
        # scintillant), not proximity in a cell — these are homogeneous
        # in vitro binding assays.
        "MI:0905",  # amplified luminescent proximity homogeneous assay
        "MI:0099",  # scintillation proximity assay
        "MI:0047",  # far western blotting
        "MI:0413",  # electrophoretic mobility shift assay
        "MI:0412",  # electrophoretic mobility supershift assay
        "MI:0982",  # electrophoretic mobility-based method
    ),
    # Self-assembly rather than affinity between two partners.
    AGGREGATION: (
        "MI:1232",  # aggregation assay
        "MI:0947",  # bead aggregation assay
        "MI:0928",  # filter trap assay
        "MI:0953",  # polymerization
    ),
    ARRAYS: (
        "MI:0089",  # protein array
        "MI:0081",  # peptide array
        "MI:0678",  # antibody array
        "MI:0008",  # array technology
        "MI:0034",  # display technology
        "MI:0084",  # phage display
        "MI:0048",  # filamentous phage display
        "MI:0900",  # p8 filamentous phage display
        "MI:0066",  # lambda phage display
        "MI:0108",  # t7 phage display
        "MI:0073",  # mrna display
        "MI:0115",  # yeast display
    ),
    ENZYMATIC: (
        "MI:0415",  # enzymatic study
        "MI:0424",  # protein kinase assay
        "MI:0423",  # in-gel kinase assay
        # Kinase-activity assays named after their readout technology; both
        # descend from MI:0424 protein kinase assay in the ontology.
        "MI:0420",  # kinase homogeneous time resolved fluorescence
        "MI:0425",  # kinase scintillation proximity assay
        "MI:0434",  # phosphatase assay
        "MI:1019",  # protein phosphatase assay
        "MI:0841",  # phosphotransferase assay
        "MI:0889",  # acetylase assay
        "MI:0406",  # deacetylase assay
        "MI:0515",  # methyltransferase assay
        "MI:0516",  # methyltransferase radiometric assay
        "MI:0870",  # demethylase assay
        "MI:0997",  # ubiquitinase assay
        "MI:0998",  # deubiquitinase assay
        "MI:1008",  # sumoylase assay
        "MI:1010",  # neddylase assay
        "MI:1005",  # adp ribosylase assay
        "MI:1309",  # de-ADP-ribosylation assay
        "MI:1007",  # glycosylase assay
        "MI:1002",  # myristoylase assay
        "MI:1004",  # palmitoylase assay
        "MI:1000",  # hydroxylase assay
        "MI:2404",  # oxidase assay
        "MI:0979",  # oxidoreductase assay
        "MI:0880",  # atpase assay
        "MI:0419",  # gtpase assay
        "MI:0949",  # gdp/gtp exchange assay
        "MI:1036",  # nucleotide exchange assay
        "MI:1138",  # decarboxylation assay
        "MI:1147",  # ampylation assay
        "MI:0605",  # enzymatic footprinting
        "MI:0417",  # footprinting
        # Limited proteolysis used to probe accessibility, not an assay of
        # cleavage activity — stays with the footprinting terms above.
        "MI:0814",  # protease accessibility laddering
    ),
    # Mirrors the CLEAVAGE interaction-type bucket below.
    CLEAVAGE_ASSAYS: (
        "MI:0435",  # protease assay
        "MI:0512",  # zymography
        "MI:0990",  # cleavage assay
        "MI:0991",  # lipoprotein cleavage assay
        "MI:1034",  # nuclease assay
        "MI:0920",  # ribonuclease assay
    ),
    # Near-root ontology terms carrying no method-specific information: MI:0401
    # is the ancestor of the affinity, array, chromatography and enzymatic
    # branches alike, so it cannot be attributed to any one of them.
    OTHER: (
        "MI:0045",  # experimental interaction detection
        "MI:0686",  # unspecified method
        "MI:0401",  # biochemical
        "MI:0013",  # biophysical
        "MI:0091",  # chromatography technology
        "MI:0256",  # rna interference
    ),
}

# ── Interaction types (keyed by lowercased name — they carry no PSI-MI code) ─

DIRECT = "Direct interaction"
ASSOCIATION = "Association & complex membership"
SELF = "Self-interaction"
SPATIAL = "Spatial proximity"
PTM_REACTIONS = "Enzymatic & post-translational reactions"
CLEAVAGE = "Cleavage"

# Alphabetical, OTHER last.
INTERACTION_TYPE_CATEGORY_ORDER: tuple[str, ...] = (
    ASSOCIATION,
    CLEAVAGE,
    DIRECT,
    PTM_REACTIONS,
    SELF,
    SPATIAL,
    OTHER,
)

_INTERACTION_TYPE_NAMES: dict[str, tuple[str, ...]] = {
    # Direct vs. association is the most consequential distinction in this
    # vocabulary, so the two get separate groups rather than one.
    DIRECT: (
        "direct interaction",
        "covalent binding",
        "disulfide bond",
    ),
    ASSOCIATION: (
        "association",
        "physical association",
    ),
    SELF: (
        "self interaction",
        "putative self interaction",
    ),
    SPATIAL: (
        "colocalization",
        "proximity",
    ),
    PTM_REACTIONS: (
        "acetylation reaction",
        "deacetylation reaction",
        "adp ribosylation reaction",
        "de-adp-ribosylation reaction",
        "ampylation reaction",
        "atpase reaction",
        "demethylation reaction",
        "methylation reaction",
        "deneddylation reaction",
        "neddylation reaction",
        "dephosphorylation reaction",
        "phosphorylation reaction",
        "phosphotransfer reaction",
        "deubiquitination reaction",
        "ubiquitination reaction",
        "enzymatic reaction",
        "glycosylation reaction",
        "gtpase reaction",
        "guanine nucleotide exchange factor reaction",
        "hydroxylation reaction",
        "lipid addition",
        "myristoylation reaction",
        "palmitoylation reaction",
        "oxidoreductase activity electron transfer reaction",
        # Note the double space — the controlled-vocabulary name really is
        # "proline isomerization  reaction".
        "proline isomerization  reaction",
        "sumoylation reaction",
        "transglutamination reaction",
    ),
    CLEAVAGE: (
        "cleavage reaction",
        "protein cleavage",
        "dna cleavage",
        "rna cleavage",
        "lipoprotein cleavage reaction",
    ),
}

# ── Source databases (keyed by lowercased name) ─────────────────────────────

INTERACTION_DB = "Interaction databases"
CURATION_TEAM = "Curation teams"
ANNOTATION_DB = "Curated annotation resources"
ARCHIVE_DB = "Structural & proteomics archives"
LITERATURE_DB = "Literature"

# Alphabetical, OTHER last.
SOURCE_CATEGORY_ORDER: tuple[str, ...] = (
    ANNOTATION_DB,
    CURATION_TEAM,
    INTERACTION_DB,
    LITERATURE_DB,
    ARCHIVE_DB,
    OTHER,
)

_SOURCE_NAMES: dict[str, tuple[str, ...]] = {
    INTERACTION_DB: (
        "intact",
        "biogrid",
        "mint",
        "dip",
        "i2d",
        "innatedb",
        "hpidb",
        "matrixdb",
        "mpidb",
        "flybase",
        "imex",
    ),
    # IMEx/IntAct curation groups rather than resources of their own. UniProt is
    # also an IMEx curation partner but is kept under ANNOTATION_DB because it
    # is far more often seen here as a cross-referenced resource.
    CURATION_TEAM: (
        "bhf-ucl",
        "mbinfo",
        "molecular connections",
        "ntnu",
    ),
    ANNOTATION_DB: (
        "uniprot",
        "go",
        "interpro",
        "omim",
        "brenda",
        "efo",
        "tissue list",
    ),
    ARCHIVE_DB: (
        "pdbe",
        "rcsb pdb",
        "wwpdb",
        "emdb",
        "empiar",
        "bmrb",
        "pride",
        "proteomexchange",
    ),
    LITERATURE_DB: ("pmc",),
}


# ── GTEx tissues (keyed by the organ prefix of the GTEx name) ───────────────
# GTEx names are "Organ - Region [- Cell type]" ("Colon - Transverse - Mucosa")
# or a bare organ ("Liver", "Lung"). Mapping the ~30 organ prefixes rather than
# all 70 tissue names keeps the registry small and means a new sub-region of a
# known organ is categorised automatically.

NERVOUS = "Nervous system"
CARDIOVASCULAR = "Cardiovascular, blood & immune"
DIGESTIVE = "Digestive system"
REPRODUCTIVE = "Reproductive & breast"
URINARY = "Urinary system"
RESPIRATORY = "Respiratory system"
MUSCULOSKELETAL = "Musculoskeletal, skin & adipose"
ENDOCRINE = "Endocrine system"
CULTURED = "Cultured cells"

# Alphabetical, OTHER last.
TISSUE_CATEGORY_ORDER: tuple[str, ...] = (
    CARDIOVASCULAR,
    CULTURED,
    DIGESTIVE,
    ENDOCRINE,
    MUSCULOSKELETAL,
    NERVOUS,
    REPRODUCTIVE,
    RESPIRATORY,
    URINARY,
    OTHER,
)

_TISSUE_PREFIXES: dict[str, tuple[str, ...]] = {
    NERVOUS: ("brain", "nerve"),
    CARDIOVASCULAR: ("artery", "heart", "whole blood", "spleen"),
    DIGESTIVE: (
        "colon",
        "esophagus",
        "small intestine",
        "stomach",
        "liver",
        "pancreas",
        "minor salivary gland",
    ),
    RESPIRATORY: ("lung",),
    URINARY: ("kidney", "bladder"),
    REPRODUCTIVE: (
        "breast",
        "cervix",
        "fallopian tube",
        "ovary",
        "prostate",
        "testis",
        "uterus",
        "vagina",
    ),
    ENDOCRINE: ("adrenal gland", "thyroid", "pituitary"),
    MUSCULOSKELETAL: ("muscle", "skin", "adipose"),
    CULTURED: ("cells",),
}


def _invert(mapping: dict[str, tuple[str, ...]]) -> dict[str, str]:
    """``{category: (key, …)}`` → ``{key: category}``."""
    return {key: category for category, keys in mapping.items() for key in keys}


_EXPERIMENT_BY_CODE: dict[str, str] = _invert(_EXPERIMENT_CODES)
_INTERACTION_TYPE_BY_NAME: dict[str, str] = _invert(_INTERACTION_TYPE_NAMES)
_SOURCE_BY_NAME: dict[str, str] = _invert(_SOURCE_NAMES)
_TISSUE_PREFIXES_BY_NAME: dict[str, str] = _invert(_TISSUE_PREFIXES)


def tissue_prefix(name: str) -> str:
    """Organ prefix of a GTEx tissue name (``"Colon - Sigmoid"`` → ``"Colon"``)."""
    return (name or "").split(" - ")[0].strip()


def tissue_category(name: str) -> str:
    """Body system for a GTEx tissue, matched on its organ prefix."""
    return _TISSUE_PREFIXES_BY_NAME.get(tissue_prefix(name).lower(), OTHER)


def experiment_category(psi_mi_code: str, name: str = "") -> str:
    """Category for an ExperimentType. ``name`` is unused today but accepted so
    callers need not care which key the lookup uses."""
    del name  # keyed on the stable PSI-MI code, not the rewritable display name
    return _EXPERIMENT_BY_CODE.get((psi_mi_code or "").strip().upper(), OTHER)


def interaction_type_category(name: str) -> str:
    """Category for an InteractionType, matched case-insensitively on name."""
    return _INTERACTION_TYPE_BY_NAME.get((name or "").strip().lower(), OTHER)


def source_category(name: str) -> str:
    """Category for a Source, matched case-insensitively on name."""
    return _SOURCE_BY_NAME.get((name or "").strip().lower(), OTHER)
