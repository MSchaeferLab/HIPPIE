"""
Django management command: seed_test_data

Place at: hippie/management/commands/seed_test_data.py
Run with: python manage.py seed_test_data
           python manage.py seed_test_data --flush   # wipe first, then seed

Creates a small but interconnected dataset covering every model and
relationship in the HIPPIE schema.  Designed so that every query path
exercised by the old PHP app (single-protein lookup, network query,
tissue filter, GO/MeSH filter, detail page, browse, shortest path
endpoints, orthologs, bait-prey) hits real rows.
"""

import random
from django.core.management.base import BaseCommand
from django.db import transaction

# Late-import models inside handle() so Django app registry is ready.

# ---------------------------------------------------------------------------
# Realistic-ish constants
# ---------------------------------------------------------------------------

GENE_SYMBOLS = [
    "BRCA1", "BRCA2", "TP53", "EGFR", "BRAF", "MAP2K1", "MAPK3",
    "HTT", "AKT1", "PTEN", "MYC", "JUN", "FOS", "KRAS", "NRAS",
    "PIK3CA", "RB1", "CDKN2A", "ERBB2", "CDH1", "SMAD4", "APC",
    "VHL", "MTOR", "JAK2", "STAT3", "SRC", "ABL1", "RAF1", "ELK1",
]

UNIPROT_IDS = {
    "BRCA1": "BRCA1_HUMAN", "BRCA2": "BRCA2_HUMAN", "TP53": "P53_HUMAN",
    "EGFR": "EGFR_HUMAN",   "BRAF": "BRAF_HUMAN",   "MAP2K1": "MP2K1_HUMAN",
    "MAPK3": "MK03_HUMAN",  "HTT": "HTT_HUMAN",     "AKT1": "AKT1_HUMAN",
    "PTEN": "PTEN_HUMAN",   "MYC": "MYC_HUMAN",     "JUN": "JUN_HUMAN",
    "FOS": "FOS_HUMAN",     "KRAS": "KRAS_HUMAN",   "NRAS": "NRAS_HUMAN",
    "PIK3CA": "PK3CA_HUMAN","RB1": "RB_HUMAN",      "CDKN2A": "CD2A2_HUMAN",
    "ERBB2": "ERBB2_HUMAN", "CDH1": "CADH1_HUMAN",  "SMAD4": "SMAD4_HUMAN",
    "APC": "APC_HUMAN",     "VHL": "VHL_HUMAN",     "MTOR": "MTOR_HUMAN",
    "JAK2": "JAK2_HUMAN",   "STAT3": "STAT3_HUMAN", "SRC": "SRC_HUMAN",
    "ABL1": "ABL1_HUMAN",   "RAF1": "RAF1_HUMAN",   "ELK1": "ELK1_HUMAN",
}

ACCESSIONS = {
    "BRCA1": "P38398", "BRCA2": "P51587", "TP53": "P04637",
    "EGFR": "P00533",  "BRAF": "P15056",  "MAP2K1": "Q02750",
    "MAPK3": "P27361", "HTT": "P42858",   "AKT1": "P31749",
    "PTEN": "P60484",  "MYC": "P01106",   "JUN": "P05412",
    "FOS": "P01100",   "KRAS": "P01116",  "NRAS": "P01111",
    "PIK3CA": "P42336","RB1": "P06400",   "CDKN2A": "P42771",
    "ERBB2": "P04626", "CDH1": "P12830",  "SMAD4": "Q13315",
    "APC": "P25054",   "VHL": "P40337",   "MTOR": "P42345",
    "JAK2": "O60674",  "STAT3": "P40763", "SRC": "P12931",
    "ABL1": "P00519",  "RAF1": "P04049",  "ELK1": "P19419",
}

# Entrez gene IDs (real)
ENTREZ_IDS = {
    "BRCA1": 672,    "BRCA2": 675,    "TP53": 7157,
    "EGFR": 1956,    "BRAF": 673,     "MAP2K1": 5604,
    "MAPK3": 5595,   "HTT": 3064,     "AKT1": 207,
    "PTEN": 5728,    "MYC": 4609,     "JUN": 3725,
    "FOS": 2353,     "KRAS": 3845,    "NRAS": 4893,
    "PIK3CA": 5290,  "RB1": 5925,     "CDKN2A": 1029,
    "ERBB2": 2064,   "CDH1": 999,     "SMAD4": 4089,
    "APC": 324,      "VHL": 7428,     "MTOR": 2475,
    "JAK2": 3717,    "STAT3": 6774,   "SRC": 6714,
    "ABL1": 25,      "RAF1": 5894,    "ELK1": 2002,
}

TISSUES = [
    "Adipose Tissue", "Adrenal Gland", "Blood", "Blood Vessel",
    "Brain", "Breast", "Colon", "Esophagus", "Heart", "Kidney",
    "Liver", "Lung", "Muscle", "Nerve", "Ovary", "Pancreas",
    "Pituitary", "Prostate", "Skin", "Stomach",
]

SOURCES = [
    ("BioGRID", "https://thebiogrid.org/"),
    ("IntAct", "https://www.ebi.ac.uk/intact/"),
    ("MINT", "https://mint.bio.uniroma2.it/"),
    ("HPRD", "http://www.hprd.org/"),
    ("DIP", "https://dip.doe-mbi.ucla.edu/"),
    ("HomoMINT", "https://mint.bio.uniroma2.it/"),  # source_id will be 6
]

EXPERIMENT_TYPES = [
    ("Two-hybrid", "MI:0018", 5.0),
    ("Affinity Capture-MS", "MI:0004", 5.0),
    ("coimmunoprecipitation", "MI:0019", 5.0),
    ("pull down", "MI:0096", 2.5),
    ("x-ray crystallography", "MI:0114", 10.0),
    ("fluorescent resonance energy transfer", "MI:0055", 6.0),
    ("surface plasmon resonance", "MI:0107", 10.0),
    ("no experiment assigned", "", 0.0),
]

INTERACTION_TYPES = [
    ("association", "MI:0914"),
    ("physical association", "MI:0915"),
    ("direct interaction", "MI:0407"),
    ("colocalization", "MI:0403"),
]

SPECIES = [
    "Homo sapiens", "Mus musculus", "Rattus norvegicus",
    "Drosophila melanogaster", "Caenorhabditis elegans",
    "Saccharomyces cerevisiae", "Danio rerio",
]

GO_TERMS = [
    # Biological Process
    ("GO:0008150", "biological_process", "biological_process"),
    ("GO:0007165", "signal transduction", "biological_process"),
    ("GO:0006468", "protein phosphorylation", "biological_process"),
    ("GO:0006915", "apoptotic process", "biological_process"),
    ("GO:0008283", "cell proliferation", "biological_process"),
    ("GO:0007049", "cell cycle", "biological_process"),
    # Cellular Component
    ("GO:0005575", "cellular_component", "cellular_component"),
    ("GO:0005634", "nucleus", "cellular_component"),
    ("GO:0005737", "cytoplasm", "cellular_component"),
    ("GO:0005886", "plasma membrane", "cellular_component"),
    ("GO:0005829", "cytosol", "cellular_component"),
]

# parent → children mapping for GO hierarchy
GO_HIERARCHY = {
    "GO:0008150": ["GO:0007165", "GO:0006468", "GO:0006915", "GO:0008283", "GO:0007049"],
    "GO:0007165": ["GO:0006468"],
    "GO:0005575": ["GO:0005634", "GO:0005737", "GO:0005886", "GO:0005829"],
    "GO:0005737": ["GO:0005829"],
}

MESH_TERMS = [
    ("C04", "Neoplasms"),
    ("C04.588", "Neoplasms by Site"),
    ("C04.588.180", "Breast Neoplasms"),
    ("C04.588.274", "Digestive System Neoplasms"),
    ("C04.588.274.476", "Gastrointestinal Neoplasms"),
    ("C10", "Nervous System Diseases"),
    ("C10.574", "Neurodegenerative Diseases"),
]


class Command(BaseCommand):
    help = "Populate all HIPPIE tables with interconnected test data."

    def add_arguments(self, parser):
        parser.add_argument(
            "--flush",
            action="store_true",
            help="Delete all existing data before seeding.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        from hippie_website.models import (
            Protein, Isoform, ProteinUniProt, ProteinEntrez,
            UniProtAccession, Tissue, ProteinTissue, Source,
            ExperimentType, InteractionType, Species, GOSlimTerm,
            MeSHTerm, Interaction, InteractionPublication,
            InteractionCrossReference, SignalingEndpoint,
            OrthologInteraction, BaitPreyAssociation,
        )

        if options["flush"]:
            self.stdout.write("Flushing existing data…")
            for M in [
                BaitPreyAssociation, InteractionCrossReference,
                InteractionPublication, OrthologInteraction,
                Interaction, SignalingEndpoint, ProteinTissue,
                Isoform, ProteinEntrez, ProteinUniProt,
                UniProtAccession, Protein, Tissue, Source,
                ExperimentType, InteractionType, Species,
                GOSlimTerm, MeSHTerm,
            ]:
                M.objects.all().delete()
            self.stdout.write(self.style.SUCCESS("  Done."))

        random.seed(42)  # reproducible

        # ---------------------------------------------------------------
        # 1. Lookup / reference tables
        # ---------------------------------------------------------------

        self.stdout.write("Creating lookup tables…")

        tissues = {t.name: t for t in [
            Tissue.objects.get_or_create(name=n)[0] for n in TISSUES
        ]}

        sources = {}
        for name, url in SOURCES:
            s, _ = Source.objects.get_or_create(name=name, defaults={"url": url})
            sources[name] = s
        # HomoMINT must be pk=6 for the HomoMINTLinkManager
        homomint_src = sources["HomoMINT"]

        exp_types = {}
        for name, psi, score in EXPERIMENT_TYPES:
            et, _ = ExperimentType.objects.get_or_create(
                name=name, defaults={"psi_mi_code": psi, "quality_score": score}
            )
            exp_types[name] = et

        int_types = {}
        for name, psi in INTERACTION_TYPES:
            it, _ = InteractionType.objects.get_or_create(
                name=name, defaults={"psi_mi_code": psi}
            )
            int_types[name] = it

        species_objs = {}
        for name in SPECIES:
            sp, _ = Species.objects.get_or_create(name=name)
            species_objs[name] = sp

        # ---------------------------------------------------------------
        # 2. GO slim terms + hierarchy
        # ---------------------------------------------------------------

        self.stdout.write("Creating GO slim terms…")

        go_objs = {}
        for go_id, name, ns in GO_TERMS:
            obj, _ = GOSlimTerm.objects.get_or_create(
                id=go_id, defaults={"name": name, "namespace": ns}
            )
            go_objs[go_id] = obj

        for parent_id, child_ids in GO_HIERARCHY.items():
            parent = go_objs[parent_id]
            children = [go_objs[c] for c in child_ids]
            parent.children.set(children)

        # ---------------------------------------------------------------
        # 3. MeSH terms
        # ---------------------------------------------------------------

        self.stdout.write("Creating MeSH terms…")

        mesh_objs = {}
        for number, name in MESH_TERMS:
            obj, _ = MeSHTerm.objects.get_or_create(
                number=number, defaults={"name": name}
            )
            mesh_objs[number] = obj

        # ---------------------------------------------------------------
        # 4. Proteins + identifier mappings
        # ---------------------------------------------------------------

        self.stdout.write("Creating proteins and identifier mappings…")

        proteins = {}
        for symbol in GENE_SYMBOLS:
            p, _ = Protein.objects.get_or_create(name=symbol)
            proteins[symbol] = p

            ProteinUniProt.objects.get_or_create(
                protein=p, uniprot_id=UNIPROT_IDS[symbol],
                defaults={"version": 1},
            )

            ProteinEntrez.objects.get_or_create(
                protein=p, gene_id=ENTREZ_IDS[symbol],
                defaults={"name": symbol},
            )

            UniProtAccession.objects.get_or_create(
                accession=ACCESSIONS[symbol],
                uniprot_id=UNIPROT_IDS[symbol],
            )

        # Isoforms for a few proteins (MTI — each Isoform IS a Protein row)
        isoforms = {}
        for symbol in ["BRCA1", "TP53", "EGFR"]:
            for iso_num in range(2, 4):
                iso_uniprot = f"{ACCESSIONS[symbol]}-{iso_num}"
                isoform, _ = Isoform.objects.get_or_create(
                    isoform_uniprot_id=iso_uniprot,
                    defaults={
                        "name": f"{symbol} isoform {iso_num}",  # Protein.name
                    },
                )
                isoforms[iso_uniprot] = isoform


        # ---------------------------------------------------------------
        # 5. Tissue expression  (each protein in 3–8 random tissues)
        # ---------------------------------------------------------------

        self.stdout.write("Creating tissue expression…")

        tissue_list = list(tissues.values())
        for p in proteins.values():
            for t in random.sample(tissue_list, k=random.randint(3, 8)):
                ProteinTissue.objects.get_or_create(protein=p, tissue=t)

        # ---------------------------------------------------------------
        # 6. Interactions  (~80 interactions, canonical order enforced)
        # ---------------------------------------------------------------

        self.stdout.write("Creating interactions…")

        symbol_list = list(GENE_SYMBOLS)
        interaction_objs = []
        created_pairs = set()

        # Well-known signaling chain: BRAF → MAP2K1 → MAPK3 → ELK1/MYC/JUN
        known_pairs = [
            ("BRAF", "MAP2K1"), ("MAP2K1", "MAPK3"), ("MAPK3", "ELK1"),
            ("MAPK3", "MYC"), ("MAPK3", "JUN"), ("BRCA1", "TP53"),
            ("BRCA1", "BRCA2"), ("EGFR", "ERBB2"), ("EGFR", "SRC"),
            ("KRAS", "BRAF"), ("KRAS", "RAF1"), ("PIK3CA", "AKT1"),
            ("AKT1", "MTOR"), ("PTEN", "AKT1"), ("JAK2", "STAT3"),
            ("TP53", "MDM2") if "MDM2" in proteins else ("TP53", "RB1"),
            ("ABL1", "SRC"), ("FOS", "JUN"),
        ]

        def make_interaction(sym1, sym2, score=None):
            p1, p2 = proteins[sym1], proteins[sym2]
            # Enforce canonical order (protein_1_id <= protein_2_id)
            if p1.pk > p2.pk:
                p1, p2 = p2, p1
            pair = (p1.pk, p2.pk)
            if pair in created_pairs:
                return None
            created_pairs.add(pair)

            if score is None:
                score = round(random.uniform(0.3, 0.99), 4)

            # ~30% get direction/effect annotations
            kegg_dir = random.choice([1, -1, None, None, None])
            effect_type = None
            effect_source = None
            if random.random() < 0.3:
                effect_type = random.choice([1, -1])
                effect_source = random.choice([1, 25])

            obj, created = Interaction.objects.get_or_create(
                protein_1=p1, protein_2=p2,
                defaults={
                    "score": score,
                    "kegg_direction": kegg_dir,
                    "effect_type": effect_type,
                    "effect_source": effect_source,
                },
            )
            if created:
                interaction_objs.append(obj)
            return obj

        # Known pairs first (high scores)
        for sym1, sym2 in known_pairs:
            if sym1 in proteins and sym2 in proteins:
                make_interaction(sym1, sym2, score=round(random.uniform(0.7, 0.99), 4))

        # Random pairs to fill out the network
        while len(interaction_objs) < 80:
            s1, s2 = random.sample(symbol_list, 2)
            make_interaction(s1, s2)

        # ---------------------------------------------------------------
        # 7. M2M evidence on interactions
        # ---------------------------------------------------------------

        self.stdout.write("Adding evidence to interactions…")

        source_list = list(sources.values())
        exp_list = list(exp_types.values())
        int_type_list = list(int_types.values())
        species_list = list(species_objs.values())
        go_list = [go_objs[gid] for gid, _, ns in GO_TERMS if gid not in ("GO:0008150", "GO:0005575")]
        mesh_list = list(mesh_objs.values())

        for inter in interaction_objs:
            # 1–3 sources
            inter.sources.set(random.sample(source_list, k=random.randint(1, 3)))
            # 1–3 experiment types
            inter.experiments.set(random.sample(exp_list, k=random.randint(1, 3)))
            # 1–2 interaction types
            inter.interaction_types.set(random.sample(int_type_list, k=random.randint(1, 2)))
            # 0–3 conserved species
            if random.random() < 0.6:
                inter.conserved_species.set(
                    random.sample(species_list, k=random.randint(1, 3))
                )
            # 1–3 GO terms
            inter.go_terms.set(random.sample(go_list, k=random.randint(1, 3)))
            # 0–2 MeSH terms
            if random.random() < 0.5:
                inter.mesh_terms.set(random.sample(mesh_list, k=random.randint(1, 2)))

        # Isoform-Isoform interactions (reuses the same Interaction table)
        # We pick pairs where both proteins have at least 2 isoforms
        iso_pairs = [
            (f"{ACCESSIONS['BRCA1']}-2", f"{ACCESSIONS['TP53']}-2"),
            (f"{ACCESSIONS['BRCA1']}-3", f"{ACCESSIONS['TP53']}-3"),
            (f"{ACCESSIONS['EGFR']}-2", f"{ACCESSIONS['BRCA1']}-2"),
        ]
        for iso_a_id, iso_b_id in iso_pairs:
            iso_a = isoforms[iso_a_id]
            iso_b = isoforms[iso_b_id]
            # Canonical ordering is on Protein pk, which isoforms inherit
            p1, p2 = (iso_a, iso_b) if iso_a.pk <= iso_b.pk else (iso_b, iso_a)
            ix, created = Interaction.objects.get_or_create(
                protein_1=p1,
                protein_2=p2,
                defaults={"score": round(random.uniform(0.5, 0.95), 4)},
            )
            if created:
                ix.sources.set(random.sample(source_list, k=random.randint(1, 2)))
                ix.experiments.set(random.sample(exp_list, k=random.randint(1, 2)))
                ix.interaction_types.set(random.sample(int_type_list, k=1))
                interaction_objs.append(ix)

        # ---------------------------------------------------------------
        # 8. Publications  (1–4 PMIDs per interaction)
        # ---------------------------------------------------------------

        self.stdout.write("Creating publications…")

        pmid_pool = list(range(20000000, 20000200))
        for inter in interaction_objs:
            for pmid in random.sample(pmid_pool, k=random.randint(1, 4)):
                InteractionPublication.objects.get_or_create(
                    interaction=inter, pmid=pmid,
                )

        # ---------------------------------------------------------------
        # 9. Cross-references  (some from HomoMINT, some from others)
        # ---------------------------------------------------------------

        self.stdout.write("Creating cross-references…")

        for i, inter in enumerate(interaction_objs[:30]):
            src = homomint_src if i < 10 else random.choice(source_list)
            sp = random.choice(species_list) if random.random() < 0.8 else None
            InteractionCrossReference.objects.get_or_create(
                interaction=inter,
                link=f"MINT-{10000 + i}",
                source=src,
                defaults={"species": sp},
            )

        # ---------------------------------------------------------------
        # 10. Signaling endpoints  (receptors + TFs for shortest path)
        # ---------------------------------------------------------------

        self.stdout.write("Creating signaling endpoints…")

        receptor_symbols = ["EGFR", "ERBB2"]
        tf_symbols = ["ELK1", "MYC", "JUN", "FOS", "TP53", "STAT3"]
        both_symbols = ["SRC"]

        for sym in receptor_symbols:
            SignalingEndpoint.objects.get_or_create(
                uniprot_id=UNIPROT_IDS[sym],
                defaults={"type": "rec"},
            )
        for sym in tf_symbols:
            SignalingEndpoint.objects.get_or_create(
                uniprot_id=UNIPROT_IDS[sym],
                defaults={"type": "tf"},
            )
        for sym in both_symbols:
            SignalingEndpoint.objects.get_or_create(
                uniprot_id=UNIPROT_IDS[sym],
                defaults={"type": "b"},
            )

        # ---------------------------------------------------------------
        # 11. Ortholog interactions  (one per source type)
        # ---------------------------------------------------------------

        self.stdout.write("Creating ortholog interactions…")

        ortho_pairs = [
            ("BRCA1", "TP53", "homomint"),
            ("EGFR", "SRC", "i2d"),
            ("KRAS", "BRAF", "ortho"),
            ("AKT1", "MTOR", "homomint"),
            ("JAK2", "STAT3", "i2d"),
            ("MAP2K1", "MAPK3", "ortho"),
        ]
        non_human = [sp for sp in species_list if sp.name != "Homo sapiens"]

        for sym1, sym2, src_tag in ortho_pairs:
            p1, p2 = proteins[sym1], proteins[sym2]
            if p1.pk > p2.pk:
                p1, p2 = p2, p1
            oi, created = OrthologInteraction.objects.get_or_create(
                protein_1=p1, protein_2=p2, source=src_tag,
            )
            if created:
                oi.ortholog_species.set(random.sample(non_human, k=random.randint(1, 3)))

        # ---------------------------------------------------------------
        # 12. Bait-prey associations  (for future use)
        # ---------------------------------------------------------------

        self.stdout.write("Creating bait-prey associations…")

        for inter in interaction_objs[:15]:
            pmids = inter.publications.values_list("pmid", flat=True)[:2]
            for pmid in pmids:
                direction = random.choice([1, -1])
                BaitPreyAssociation.objects.get_or_create(
                    interaction=inter, pmid=pmid, direction=direction,
                )

        # ---------------------------------------------------------------
        # Summary
        # ---------------------------------------------------------------

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Seed complete. Summary:"))
        counts = [
            ("Protein", Protein),
            ("Isoform", Isoform),
            ("ProteinUniProt", ProteinUniProt),
            ("ProteinEntrez", ProteinEntrez),
            ("UniProtAccession", UniProtAccession),
            ("Tissue", Tissue),
            ("ProteinTissue", ProteinTissue),
            ("Source", Source),
            ("ExperimentType", ExperimentType),
            ("InteractionType", InteractionType),
            ("Species", Species),
            ("GOSlimTerm", GOSlimTerm),
            ("MeSHTerm", MeSHTerm),
            ("Interaction", Interaction),
            ("InteractionPublication", InteractionPublication),
            ("InteractionCrossReference", InteractionCrossReference),
            ("SignalingEndpoint", SignalingEndpoint),
            ("OrthologInteraction", OrthologInteraction),
            ("BaitPreyAssociation", BaitPreyAssociation),
        ]
        for label, model in counts:
            self.stdout.write(f"  {label:30s} {model.objects.count():>5}")
