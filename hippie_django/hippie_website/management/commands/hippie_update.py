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
    Source,
    Species,
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
    p1_id: int
    p2_id: int
    pmids: set[int] = field(default_factory=set)
    techs: set[str] = field(default_factory=set)
    types: set[str] = field(default_factory=set)
    link: str = ""

    def merge(self, other: "_ParsedRow") -> None:
        self.pmids |= other.pmids
        self.techs |= other.techs
        self.types |= other.types
        if not self.link and other.link:
            self.link = other.link


# ---------------------------------------------------------------------------
# MITAB field helpers
# ---------------------------------------------------------------------------



def _parse_pmid(field_val: str) -> tuple[int | None, bool]:
    for part in field_val.split("|"):
        if part.startswith("pubmed:"):
            return int(part.split(":")[1])
        return None


def _parse_interaction_type(field_val: str) -> int | None:
    interaction_type = field_val.split("(")[-1].rstrip(")")
    return interaction_type


def _parse_tech(field_val: str) -> tuple[str | None, bool]:
    tech = field_val.split("(")[-1].rstrip(")")
    tech = _TECH_NORM.get(tech, tech) ## Check !
    if tech in _SKIP_TECHS:
        return None, True
    else:
        return tech, False


def get_uniprot_acc_map() -> dict[str, str]:
    from hippie_website.models import ProteinSynonym
    acc_map: dict[str, str] = dict(
        ProteinSynonym.objects.values_list("synonym", "protein__uniprot_accession")
    )
    return acc_map


# ---------------------------------------------------------------------------
# Per-source file parsers
# ---------------------------------------------------------------------------


def _parse_biogrid(
    path: str,
) -> tuple[dict[tuple[int, int], _ParsedRow], int, int]:
    """Parse BioGRID MITAB file.

    Returns (pair_map, total_rows, skipped_rows).
    pair_map keys are canonical (p1_id, p2_id) with p1_id <= p2_id.
    """
    pair_map: dict[tuple[int, int], _ParsedRow] = {}
    total = skipped = 0

    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if line.startswith("#") or not line:
                continue
            cols = line.split("\t")
            if len(cols) < 14:
                skipped += 1
                continue
            total += 1

            # Human-only filter
            if cols[9] != "taxid:9606" or cols[10] != "taxid:9606":
                skipped += 1
                continue

            # Entrez IDs
            try:
                raw1 = cols[0].split("|")[0]
                raw2 = cols[1].split("|")[0]
                entrez1 = int(raw1.split(":")[1])
                entrez2 = int(raw2.split(":")[1])
            except (ValueError, IndexError):
                skipped += 1
                continue

            p1 = gene_map.get(entrez1)
            p2 = gene_map.get(entrez2)
            if p1 is None or p2 is None:
                skipped += 1
                continue



            pmid = _parse_pmid(cols[8])
            if pmid is None:
                skipped += 1
                continue

            itype = _paren_name(cols[11])
            try:
                link = cols[13].split(":")[1].split("|")[0]
            except IndexError:
                link = ""

            if p1 > p2:
                p1, p2 = p2, p1

            key = (p1, p2)
            if key in pair_map:
                row = pair_map[key]
                row.pmids.add(pmid)
                if tech:
                    row.techs.add(tech)
                if itype:
                    row.types.add(itype)
                if not row.link and link:
                    row.link = link
            else:
                pair_map[key] = _ParsedRow(
                    p1_id=p1,
                    p2_id=p2,
                    pmids={pmid},
                    techs={tech} if tech else set(),
                    types={itype} if itype else set(),
                    link=link,
                )

    return pair_map, total, skipped


def _parse_intact(
    path: str,
) -> tuple[dict[tuple[int, int], _ParsedRow], int, int]:
    
    pair_map: dict[tuple[int, int], _ParsedRow] = {}
    total = 0
    skipped = 0
    acc_map = get_uniprot_acc_map()
    gene_map = get_gene_map()
    
    
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if line.startswith("#") or not line:
                continue
            total += 1
            line = line.split("\t")
            both_human = line[9].startswith("taxid:9606") or not line[10].startswith("taxid:9606")
            if not both_human:
                skipped += 1
                continue

            has_uniprot_acc = line[0].startswith("uniprotkb") and line[1].startswith("uniprotkb")
            if not has_uniprot_acc:
                skipped += 1
                continue

            acc1 = line[0].split(":")[1].split("|")[0]
            acc2 = line[1].split(":")[1].split("|")[0]
            if "-" in acc1:
                iso1 = acc1.split("-")[1]
                acc1 = acc1.split("-")[0]
            if "-" in acc2:
                iso2 = acc2.split("-")[1]
                acc2 = acc2.split("-")[0]
            
            
            p1 = acc_map.get(acc1)
            p2 = acc_map.get(acc2)
            
            # if p1 is None or p2 is None:
            #     skipped += 1
            #     continue

            tech, skip = _parse_tech(line[6])
            if skip:
                skipped += 1
                continue

            pmid, skip = _parse_pmid(line[8])
            if skip:
                skipped += 1
                continue

            itype = _parse_interaction_type(line[11])
            try:
                link = cols[13].split(":")[1].split("|")[0]
            except IndexError:
                link = ""

            if p1 > p2:
                p1, p2 = p2, p1

            key = (p1, p2)
            if key in pair_map:
                row = pair_map[key]
                row.pmids.add(pmid)
                if tech:
                    row.techs.add(tech)
                if itype:
                    row.types.add(itype)
                if not row.link and link:
                    row.link = link
            else:
                pair_map[key] = _ParsedRow(
                    p1_id=p1,
                    p2_id=p2,
                    pmids={pmid},
                    techs={tech} if tech else set(),
                    types={itype} if itype else set(),
                    link=link,
                )

    return pair_map, total, skipped


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------


def _upsert(
    rows: list[_ParsedRow],
    source: Source,
    exp_map: dict[str, ExperimentType],
    type_map: dict[str, InteractionType],
    dry_run: bool,
) -> tuple[set[int], int, int]:
    """Insert or update interactions.

    Returns (touched_ids, new_count, updated_count).
    """
    touched: set[int] = set()
    new_count = updated_count = 0

    for row in rows:
        if dry_run:
            touched.add(0)
            continue

        interaction, created = Interaction.objects.get_or_create(
            protein_1_id=row.p1_id,
            protein_2_id=row.p2_id,
            defaults={"score": 0.0},
        )
        touched.add(interaction.pk)

        if created:
            new_count += 1
        else:
            updated_count += 1

        interaction.sources.add(source)

        for pmid in row.pmids:
            pub, _ = Publication.objects.get_or_create(pmid=pmid)
            interaction.publications.add(pub)

        for tech in row.techs:
            et = exp_map.get(tech)
            if et is not None:
                interaction.experiments.add(et)

        for itype in row.types:
            it = type_map.get(itype)
            if it is not None:
                interaction.interaction_types.add(it)

        if row.link:
            InteractionCrossReference.objects.get_or_create(
                interaction=interaction,
                link=row.link,
                source=source,
                defaults={"species": None},
            )

    return touched, new_count, updated_count


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
        parser.add_argument(
            "--batch-size",
            type=int,
            default=1000,
            metavar="N",
            help="Batch size for bulk_update (default: 1000)",
        )

    def handle(self, *_args: object, **options: object) -> None:
        biogrid_path: str | None = options["biogrid"]  # type: ignore[assignment]
        intact_path: str | None = options["intact"]  # type: ignore[assignment]
        rescore_all: bool = options["rescore_all"]  # type: ignore[assignment]
        dry_run: bool = options["dry_run"]  # type: ignore[assignment]
        batch_size: int = options["batch_size"]  # type: ignore[assignment]

        if not biogrid_path and not intact_path:
            self.stderr.write("Provide at least one of --biogrid or --intact.")
            return

        if dry_run:
            self.stdout.write("[dry-run] No changes will be written to the database.")

        # ------------------------------------------------------------------
        # 1. Preload lookup maps
        # ------------------------------------------------------------------
        self.stdout.write("Loading lookup maps...")

        exp_map: dict[str, ExperimentType] = {
            et.name: et for et in ExperimentType.objects.all()
        }
        type_map: dict[str, InteractionType] = {
            it.name: it for it in InteractionType.objects.all()
        }

        # uniprot_accession → Protein pk
        from hippie_website.models import Protein

        acc_map: dict[str, int] = dict(
            Protein.objects.values_list("uniprot_accession", "pk")
        )

        # entrez_id → first Protein pk for that gene
        from hippie_website.models import Gene

        gene_map: dict[int, int] = {}
        for gid, pid in (
            Gene.objects.prefetch_related("proteins")
            .values_list("entrez_id", "proteins__pk")
            .exclude(proteins__pk=None)
        ):
            gene_map.setdefault(gid, pid)

        human_species_ids: list[int] = list(
            Species.objects.filter(name__icontains="homo sapiens").values_list(
                "pk", flat=True
            )
        )

        self.stdout.write(
            f"  {len(exp_map)} experiment types, {len(type_map)} interaction types, "
            f"{len(acc_map)} proteins by accession, {len(gene_map)} genes"
        )

        # ------------------------------------------------------------------
        # 2. Parse and upsert BioGRID
        # ------------------------------------------------------------------
        touched_ids: set[int] = set()

        if biogrid_path:
            biogrid_source, _ = (
                (None, None)
                if dry_run
                else Source.objects.get_or_create(name="BioGRID")
            )
            self.stdout.write(f"Parsing BioGRID: {biogrid_path}")
            pair_map, total, skipped = _parse_biogrid(biogrid_path, gene_map)
            rows = list(pair_map.values())
            self.stdout.write(
                f"  {total} rows read → {len(rows)} unique pairs, "
                f"{skipped} skipped (no protein / no PMID / non-human / excluded tech)"
            )
            if not dry_run and biogrid_source is not None:
                ids, new_c, upd_c = _upsert(
                    rows, biogrid_source, exp_map, type_map, dry_run
                )
                touched_ids |= ids
                self.stdout.write(f"  Upserted: {new_c} new, {upd_c} updated")
            else:
                self.stdout.write("  [dry-run] skipping DB writes")

        # ------------------------------------------------------------------
        # 3. Parse and upsert IntAct
        # ------------------------------------------------------------------
        if intact_path:
            intact_source, _ = (
                (None, None) if dry_run else Source.objects.get_or_create(name="IntAct")
            )
            self.stdout.write(f"Parsing IntAct: {intact_path}")
            pair_map, total, skipped = _parse_intact(intact_path, acc_map)
            rows = list(pair_map.values())
            self.stdout.write(
                f"  {total} rows read → {len(rows)} unique pairs, "
                f"{skipped} skipped (no protein / no PMID / non-human / non-uniprot / excluded tech)"
            )
            if not dry_run and intact_source is not None:
                ids, new_c, upd_c = _upsert(
                    rows, intact_source, exp_map, type_map, dry_run
                )
                touched_ids |= ids
                self.stdout.write(f"  Upserted: {new_c} new, {upd_c} updated")
            else:
                self.stdout.write("  [dry-run] skipping DB writes")

        # ------------------------------------------------------------------
        # 4. Rescore
        # ------------------------------------------------------------------
        if dry_run:
            self.stdout.write("[dry-run] Skipping rescore step.")
            self.stdout.write("Done.")
            return

        if rescore_all:
            score_ids = list(Interaction.objects.values_list("pk", flat=True))
        else:
            score_ids = list(touched_ids - {0})

        if score_ids:
            self.stdout.write(f"Rescoring {len(score_ids)} interactions...")
            _rescore(score_ids, batch_size, human_species_ids)
        else:
            self.stdout.write("No interactions to rescore.")

        self.stdout.write("Done.")
