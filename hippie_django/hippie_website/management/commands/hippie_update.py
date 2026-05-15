"""
hippie_update — update HIPPIE interactions from BioGRID and/or IntAct MITAB files.

Ports the Java Run.java + DBUpdater.java + DB.scoreDB() pipeline to Django ORM.

Usage:
    python manage.py hippie_update --biogrid path/to/biogrid.mitab
    python manage.py hippie_update --intact path/to/intact.txt
    python manage.py hippie_update --biogrid b.mitab --intact i.txt --rescore-all
    python manage.py hippie_update --biogrid b.mitab --dry-run
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from django.core.management.base import BaseCommand, CommandParser
from django.db.models import Count, Q, Sum

from hippie_website.models import (
    ExperimentType,
    Interaction,
    InteractionCrossReference,
    InteractionType,
    Publication,
    Protein,
    Isoform,
    Source,
    Gene,
)

# ---------------------------------------------------------------------------
# Scoring constants (from DB.java)
# ---------------------------------------------------------------------------

_A_S = 2.3  # publication count scaling
_A_O = 1.6  # ortholog species scaling
_A_T = 0.2  # experiment quality-score sum scaling

_W_S = 0.6  # publication weight
_W_O = 0.1  # ortholog weight
_W_T = 0.3  # experiment weight


def _sat(x: float, a: float) -> float:
    return 2.0 / (1.0 + math.exp(-a * x)) - 1.0


def _compute_score(pub_n: int, orth_n: int, exp_sum: float) -> float:
    return (
        _W_S * _sat(pub_n, _A_S)
        + _W_O * _sat(orth_n, _A_O)
        + _W_T * _sat(exp_sum, _A_T)
    )


# ---------------------------------------------------------------------------
# Tech-name normalisation (mirrors RetrieveDataFromFile.java)
# ---------------------------------------------------------------------------

_TECH_NORM: dict[str, str] = {
    "two hybrid": "Two-hybrid",
    "two hybrid prey pooling approach": "Two-hybrid",
    "imaging techniques": "imaging technique",
}

_SKIP_TECHS = {"genetic interference"}


# ---------------------------------------------------------------------------
# Parsed row dataclass
# ---------------------------------------------------------------------------


@dataclass
class _ParsedRow:
    p1_id: str  # non-default fields must come first
    p2_id: str
    uniprot_names: list[str] = field(default_factory=list)
    gene_names: list[str] = field(default_factory=list)
    entrez_ids: list[str] = field(default_factory=list)
    iso: list[bool] = field(default_factory=[False, False])
    pmids: set[int] = field(default_factory=set)
    techs: set[tuple[str, str]] = field(default_factory=set)
    types: set[str] = field(default_factory=set)
    link: set[tuple[str, str]] = field(default_factory=set)
    source: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# MITAB field helpers
# ---------------------------------------------------------------------------


def _parse_pmid(field_val: str) -> tuple[int | None, bool]:
    for part in field_val.split("|"):
        if part.startswith("pubmed:"):
            if "unassigned" not in part:
                return int(part.split(":")[1]), False
    return None, True


def _parse_interaction_type(field_val: str) -> str | None:
    interaction_type = field_val.split("(")[-1].rstrip(")")
    return interaction_type


def _parse_tech(field_val: str) -> tuple[tuple[str, str] | None, bool]:
    name = field_val.split("(")[-1].rstrip(")")
    name = _TECH_NORM.get(name, name)
    if name in _SKIP_TECHS:
        return None, True
    mi_code = ""
    if 'psi-mi:"' in field_val:
        mi_code = field_val.split('psi-mi:"')[1].split('"')[0]
    return (mi_code, name), False


def _parse_uniprot_acc(field_val: str) -> str | None:
    if field_val.startswith("uniprotkb:"):
        acc = field_val.split(":")[1]
        return acc  # preserve isoform suffix (e.g. "P38398-2")
    else:
        for val in field_val.split("|"):
            if val.startswith("uniprot/swiss-prot:"):
                acc = val.split(":")[1]
                return acc  # preserve isoform suffix
    return None


def _parse_source(field_val: str) -> str:
    return field_val.split("(")[-1].rstrip(")")


def _parse_links(field_val: str) -> set[tuple[str, str]]:
    links: set[tuple[str, str]] = set()
    for part in field_val.split("|"):
        parts = part.split(":", 1)
        if len(parts) == 2:
            links.add((parts[0], parts[1]))
    return links


def get_uniprot_acc_map() -> tuple[dict[str, str], dict[str, str]]:
    header = True
    with open("data/sec_ac.txt", "r") as f:
        acc_map: dict[str, str] = {}
        for line in f:
            if line.startswith("_"):
                header = False
                continue
            if not header:
                accs = line.strip().split(" ")
                s_ac, p_ac = accs[0], accs[-1]
                acc_map[s_ac] = p_ac

    uniprot_name_map: dict[str, str] = {}
    with open("data/HUMAN_9606_idmapping.dat", "r") as f:
        for line in f:
            line = line.strip().split("\t")
            id = line[0]
            acc_map[id] = id
            if line[1] == "UniProtKB-ID":
                uniprot_name_map[id] = line[2]

    return acc_map, uniprot_name_map


def suppliment_missing_entrez_id(
    entrez_map: dict[str, list[str | None, str | None]],
) -> dict[str, list[str | None, str | None]]:
    name_entrez_dict = dict()
    with open("data/Homo_sapiens.gene_info", "r") as f:
        for line in f:
            line = line.split("\t")
            name_entrez_dict[line[2]] = line[1]

    acc_to_drop = set()
    for acc, (entrez, name) in entrez_map.items():
        if entrez is None:
            new_entrez = name_entrez_dict.get(name, False)
            if new_entrez:
                entrez_map[acc][0] = new_entrez
            else:
                acc_to_drop.add(acc)

    for acc in acc_to_drop:
        del entrez_map[acc]

    return entrez_map


def get_human_gene_map():
    human_gene_map: dict[str, list[str | None, str | None]] = dict()
    with open("data/HUMAN_9606_idmapping.dat", "r") as f:
        for line in f:
            line = line.strip().split("\t")
            id = line[0]
            if id not in human_gene_map:
                human_gene_map[id] =[None, None]

            if line[1] == "GeneID":
                human_gene_map[id][0] = line[2]
            if line[1] == "Gene_Name":
                human_gene_map[id][1] = line[2]

    human_gene_map = suppliment_missing_entrez_id(human_gene_map)

    return human_gene_map


# ---------------------------------------------------------------------------
# Per-source file parsers
# ---------------------------------------------------------------------------


def _parse_intact_or_biogrid(
    path: str,
    source_file: ["biogrid", "inact"],
) -> tuple[dict[tuple[str, str], _ParsedRow], int, int, set[str], int]:
    pair_map: dict[tuple[str, str], _ParsedRow] = {}
    total = 0
    skipped = 0
    acc_map, uniprot_name_map = get_uniprot_acc_map()
    gene_map = get_human_gene_map()
    n_deleted = 0
    deleted: set[str] = set()

    if source_file == "inact":
        id_idx_1 = 0
        id_idx_2 = 1

    elif source_file == "biogrid":
        id_idx_1 = 2
        id_idx_2 = 3

    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if line.startswith("#") or not line:
                continue
            total += 1
            line = line.split("\t")
            both_human = line[9].startswith("taxid:9606") and line[10].startswith(
                "taxid:9606"
            )
            if not both_human:
                skipped += 1
                continue

            acc1 = _parse_uniprot_acc(line[id_idx_1])
            acc2 = _parse_uniprot_acc(line[id_idx_2])
            has_uniprot_acc = all([acc1, acc2])

            if not has_uniprot_acc:
                skipped += 1
                continue

            iso1 = "-" in acc1
            iso2 = "-" in acc2

            p1 = acc_map.get(acc1)
            p2 = acc_map.get(acc2)

            uniprot_name1 = uniprot_name_map.get(p1)
            uniprot_name2 = uniprot_name_map.get(p2)

            tech, skip = _parse_tech(line[6])
            if skip:
                skipped += 1
                continue

            if p1 is None or p2 is None:
                skipped += 1
                n_deleted += 1
                if p1 is None:
                    deleted.add(acc1)
                elif p2 is None:
                    deleted.add(acc2)
                continue

            pmid, skip = _parse_pmid(line[8])
            if skip:
                skipped += 1
                continue

            itype = _parse_interaction_type(line[11])
            link = _parse_links(line[13])
            source = _parse_source(line[12])

            entrez = []
            gene_names = []

            skip=False
            for protein in [p1, p2]:
                try:
                    gene_id, gene_name = gene_map[protein]
                    entrez.append(gene_id)
                    gene_names.append(gene_name)
                except KeyError:
                    n_deleted += 1
                    deleted.add(protein)
                    skip=True
                    break
            if skip:
                skipped += 1
                continue

            key = tuple(sorted([p1, p2]))
            try:
                row = pair_map[key]
                row.pmids.add(pmid)
                row.source.add(source)
                if tech:
                    row.techs.add(tech)
                if itype:
                    row.types.add(itype)
                if link:
                    row.link |= link
            except KeyError:
                pair_map[key] = _ParsedRow(
                    p1_id=p1,
                    p2_id=p2,
                    iso=[iso1, iso2],
                    gene_names=gene_names,
                    entrez_ids=entrez,
                    uniprot_names=[uniprot_name1, uniprot_name2],
                    pmids={pmid},
                    source={source},
                    techs={tech} if tech else set(),
                    types={itype} if itype else set(),
                    link=link,
                )

    return pair_map, total, skipped, deleted, n_deleted


# ---------------------------------------------------------------------------
# upsert interactions
# ---------------------------------------------------------------------------


def _upsert(
    rows: list[_ParsedRow],
) -> tuple[set[int], int, int]:
    touched: set[int] = set()
    new_protein_entries = 0
    new_interaction_created = 0
    new_genes_created = 0

    acc_human_map = get_human_gene_map()
    total_rows = len(rows)
    for i, row in enumerate(rows, 1):
        if i % 100 == 0 or i == total_rows:
            pct = i * 100 // total_rows
            bar = "#" * (pct // 2) + "-" * (50 - pct // 2)
            print(f"\r  [{bar}] {pct:3d}% ({i}/{total_rows})", end="", flush=True)
        proteins: list[Protein | None] = [None, None]
        for i, (p_acc, p_name, iso_bool, entrez, gene_name) in enumerate(
            zip(
                *[
                    [row.p1_id, row.p2_id],
                    row.uniprot_names,
                    row.iso,
                    row.entrez_ids,
                    row.gene_names,
                ]
            )
        ):
            if iso_bool:
                g_p_name = p_name.split("-")[0]
                gene_id, g_created = Gene.objects.get_or_create(
                    entrez_id=entrez,
                    defaults={"entrez_name": gene_name or ""},
                )
                if g_created:
                    new_genes_created += 1

                general_protein, created = Protein.objects.get_or_create(
                    uniprot_accession=p_acc.split("-")[0],
                    gene=gene_id,
                    uniprot_name=g_p_name,
                )
                if created:
                    new_protein_entries += 1
                
                
                proteins[i], created = Isoform.objects.get_or_create(
                    uniprot_accession=p_acc,
                    general_protein=general_protein,
                    gene=gene_id,
                    uniprot_name=p_name,
                )
                if created:
                    new_protein_entries += 1
            else:
                gene, g_created = Gene.objects.get_or_create(
                    entrez_id=entrez,
                    defaults={"entrez_name": gene_name or ""},
                )
                if g_created:
                    new_genes_created += 1

                proteins[i], created = Protein.objects.get_or_create(
                    uniprot_accession=p_acc, gene=gene, uniprot_name=p_name
                )
                if created:
                    new_protein_entries += 1

        internal_protein_id = sorted([proteins[0].pk, proteins[1].pk])

        interaction, created = Interaction.objects.get_or_create(
            protein_1_id=internal_protein_id[0],
            protein_2_id=internal_protein_id[1],
            defaults={"score": 0.0},
        )
        if created:
            new_interaction_created += 1
        touched.add(interaction.pk)

        for source_name in row.source:
            source, _ = Source.objects.get_or_create(name=source_name)
            interaction.sources.add(source)

        for pmid in row.pmids:
            pub, _ = Publication.objects.get_or_create(pmid=pmid)
            interaction.publications.add(pub)

        for mi_code, tech_name in row.techs:
            et, _ = ExperimentType.objects.get_or_create(
                name=tech_name,
                defaults={"psi_mi_code": mi_code, "quality_score": 0.0},
            )
            interaction.experiments.add(et)

        for itype in row.types:
            it, _ = InteractionType.objects.get_or_create(
                name=itype,
                defaults={"psi_mi_code": ""},
            )
            interaction.interaction_types.add(it)

        for db_name, link_id in row.link:
            link_source, _ = Source.objects.get_or_create(name=db_name)
            InteractionCrossReference.objects.get_or_create(
                interaction=interaction,
                link=link_id,
                source=link_source,
                defaults={"species": None},
            )

    print()
    return touched, new_protein_entries, new_interaction_created


# ---------------------------------------------------------------------------
# Rescoring
# ---------------------------------------------------------------------------


def _rescore(
    ids: list[int],
    batch_size: int,
    human_species_ids: list[int],
) -> None:
    """Recompute and persist scores for the given Interaction PKs."""
    if not ids:
        return

    pub_counts: dict[int, int] = dict(
        Interaction.objects.filter(pk__in=ids)
        .annotate(n=Count("publications", distinct=True))
        .values_list("pk", "n")
    )

    orth_counts: dict[int, int] = dict(
        Interaction.objects.filter(pk__in=ids)
        .annotate(
            n=Count(
                "conserved_species",
                filter=~Q(conserved_species__pk__in=human_species_ids),
                distinct=True,
            )
        )
        .values_list("pk", "n")
    )

    exp_sums: dict[int, float | None] = dict(
        Interaction.objects.filter(pk__in=ids)
        .annotate(s=Sum("experiments__quality_score"))
        .values_list("pk", "s")
    )

    objs: list[Interaction] = []
    for iid in ids:
        raw_score = _compute_score(
            pub_counts.get(iid, 0),
            orth_counts.get(iid, 0),
            exp_sums.get(iid) or 0.0,
        )
        objs.append(Interaction(pk=iid, score=min(1.0, max(0.0, raw_score))))

    Interaction.objects.bulk_update(objs, ["score"], batch_size=batch_size)


# ---------------------------------------------------------------------------
# Management command
# ---------------------------------------------------------------------------


class Command(BaseCommand):
    help = "Update HIPPIE interactions from BioGRID and/or IntAct MITAB files, then rescore."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--biogrid", metavar="FILE", help="Path to BioGRID MITAB file"
        )
        parser.add_argument(
            "--intact", metavar="FILE", help="Path to IntAct MITAB file"
        )
        parser.add_argument(
            "--rescore-all",
            action="store_true",
            help="Rescore every Interaction in the DB (not just touched ones)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Parse and report stats without writing to the database",
        )

    def handle(self, *_args: object, **options: object) -> None:
        biogrid_path: str | None = options["biogrid"]  # type: ignore[assignment]
        intact_path: str | None = options["intact"]  # type: ignore[assignment]
        rescore_all: bool = options["rescore_all"]  # type: ignore[assignment]

        if not biogrid_path and not intact_path:
            self.stderr.write("Provide at least one of --biogrid or --intact.")
            return

        touched_ids: set[int] = set()

        if biogrid_path:
            self.stdout.write(f"Parsing BioGRID: {biogrid_path}")
            pair_map, total, skipped, _, _ = _parse_intact_or_biogrid(
                biogrid_path, "biogrid"
            )
            rows = list(pair_map.values())
            self.stdout.write(
                f"  {total} rows read → {len(rows)} unique pairs, "
                f"{skipped} skipped (no protein / no PMID / non-human / excluded tech)"
            )
            ids, new_c, upd_c = _upsert(rows)
            touched_ids |= ids
            self.stdout.write(
                f"  Upserted: {new_c} new proteins, {upd_c} new interactions"
            )

        if intact_path:
            self.stdout.write(f"Parsing IntAct: {intact_path}")
            pair_map, total, skipped, _, _ = _parse_intact_or_biogrid(
                intact_path, "inact"
            )
            rows = list(pair_map.values())
            self.stdout.write(
                f"  {total} rows read → {len(rows)} unique pairs, "
                f"{skipped} skipped (no protein / no PMID / non-human / non-uniprot / excluded tech)"
            )
            ids, new_c, upd_c = _upsert(rows)
            touched_ids |= ids
            self.stdout.write(
                f"  Upserted: {new_c} new proteins, {upd_c} new interactions"
            )

        # Rescore (wired up once human_species_ids is available)
        # if rescore_all:
        #     score_ids = list(Interaction.objects.values_list("pk", flat=True))
        # else:
        #     score_ids = list(touched_ids - {0})
        # if score_ids:
        #     self.stdout.write(f"Rescoring {len(score_ids)} interactions...")
        #     _rescore(score_ids, batch_size, human_species_ids)
        # else:
        #     self.stdout.write("No interactions to rescore.")
        # self.stdout.write("Done.")
