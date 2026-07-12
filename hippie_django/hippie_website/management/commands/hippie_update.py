"""
hippie_update — update HIPPIE interactions from BioGRID and/or IntAct MITAB files.

Ports the Java Run.java + DBUpdater.java + DB.scoreDB() pipeline to Django ORM.

Usage:
    python manage.py hippie_update --biogrid path/to/biogrid.mitab
    python manage.py hippie_update --intact path/to/intact.txt
    python manage.py hippie_update --biogrid b.mitab --intact i.txt --rescore-all
"""

from __future__ import annotations

import contextlib
import json
import math
import re
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path

from django.core.management.base import BaseCommand, CommandParser
from django.db import connection, transaction
from django.db.models.functions import Lower

from ._mitab import open_mitab

from hippie_website.models import (
    ExperimentType,
    Gene,
    GeneSynonym,
    Interaction,
    InteractionCrossReference,
    InteractionType,
    NonInteraction,
    OrthologInteraction,
    Isoform,
    Protein,
    ProteinSynonym,
    Publication,
    ReleaseMeta,
    Source,
)
from hippie_website.stats_utils import compute_quartiles

from ._sources import data_path, load_sources

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
    "bioid": "proximity labelling technology",
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
    iso: list[bool] = field(default_factory=lambda: [False, False])
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


_CV_NAME_RE = re.compile(r"\((.*)\)")


def _extract_cv_name(field_val: str) -> str:
    """Extract a PSI-MI CV term's display name from a MITAB field.

    Matches from the first '(' to the last ')' (regex backtracking, not
    str.split) so a name containing its own literal parens — e.g. IntAct's
    'proteinchip(r) on a surface-enhanced laser desorption/ionization' —
    isn't truncated mid-word. Also strips the extra literal quote-wrapping
    IntAct sometimes adds: psi-mi:"MI:0095"("proteinchip(r) ...").
    """
    match = _CV_NAME_RE.search(field_val)
    name = match.group(1) if match else field_val
    return name.strip('"')


def _parse_interaction_type(field_val: str) -> str | None:
    return _extract_cv_name(field_val)


def _parse_tech(field_val: str) -> tuple[tuple[str, str] | None, bool]:
    name = _extract_cv_name(field_val)
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
    return _extract_cv_name(field_val)


def _parse_links(field_val: str) -> set[tuple[str, str]]:
    links: set[tuple[str, str]] = set()
    for part in field_val.split("|"):
        parts = part.split(":", 1)
        if len(parts) == 2:
            links.add((parts[0], parts[1]))
    return links


def _load_id_mappings() -> tuple[
    dict[str, str],
    dict[str, str],
    dict[str, list[str | None, str | None]],
    set[str],
    set[str],
]:
    """
    Build three lookup tables from the UniProt data files in one pass.

    Returns:
        acc_map          — secondary_acc -> primary_acc (identity for all known accessions)
        uniprot_name_map — acc -> UniProtKB-ID name
        gene_map         — acc -> [entrez_id, gene_name]
        dropped_accs     — accessions dropped because no gene could be mapped
        conflict_genes   — gene names with conflicting Entrez IDs
    """
    # Phase 1: read secondary → primary accession mapping
    acc_map: dict[str, str] = {}
    with open(data_path("sec_ac"), "r") as f:
        past_header = False
        for line in f:
            if line.startswith("_"):
                past_header = True
                continue
            if past_header:
                accs = line.strip().split(" ")
                acc_map[accs[0]] = accs[-1]

    # Phase 2: single pass over HUMAN_9606_idmapping.dat builds all three dicts
    uniprot_name_map: dict[str, str] = {}
    gene_map: dict[str, list[str | None, str | None]] = {}
    isoforms: set[str] = set()

    with open(data_path("human_idmapping"), "r") as f:
        for line in f:
            parts = line.strip().split("\t")
            id_ = parts[0]
            acc_map[id_] = id_
            if "-" in id_:
                isoforms.add(id_)
            if id_ not in gene_map:
                gene_map[id_] = [None, None]
            if parts[1] == "UniProtKB-ID":
                uniprot_name_map[id_] = parts[2]
            elif parts[1] == "GeneID":
                gene_map[id_][0] = parts[2]
            elif parts[1] == "Gene_Name":
                gene_map[id_][1] = parts[2]

    # Phase 3: derive isoform UniProt names from their canonical entry
    for iso_id in isoforms:
        if iso_id not in uniprot_name_map:
            can_id, iso_n = iso_id.split("-")
            if can_id in uniprot_name_map:
                uniprot_name_map[iso_id] = uniprot_name_map[can_id] + f"_{iso_n}"

    gene_map, dropped_accs, conflict_genes = suppliment_missing_entrez_id(gene_map)
    return acc_map, uniprot_name_map, gene_map, dropped_accs, conflict_genes


def get_human_gene_map() -> tuple[
    dict[str, list[str | None, str | None]],
    dict[str, str],
    dict[str, str],
]:
    """Return (gene_map, acc_map, uniprot_name_map) for human proteins."""
    acc_map, uniprot_name_map, gene_map, _, _ = _load_id_mappings()
    return gene_map, acc_map, uniprot_name_map


def suppliment_missing_entrez_id(entrez_map: dict[str, list[str | None, str | None]]):
    entrez_uniprot_conflict_genes = set()
    name_entrez_dict = dict()
    synonyms_dict = dict()
    with open(data_path("gene_info"), "r") as f:
        for line in f:
            line = line.split("\t")
            name_entrez_dict[line[2]] = line[1]
            for syn in line[4].split("|"):
                synonyms_dict[syn] = [line[2], line[1]]

    acc_to_drop = set()
    isoforms = set()
    for acc, (entrez, name) in entrez_map.items():
        if "-" in acc:
            isoforms.add(acc)
            continue
        elif entrez is None:
            if name:
                new_entrez = name_entrez_dict.get(name, False)
                if new_entrez:
                    entrez_map[acc][0] = new_entrez
                else:
                    gene_name, syn_entrez = synonyms_dict.get(name, [False, False])
                    if syn_entrez:
                        entrez_map[acc] = [syn_entrez, gene_name]
                    else:
                        entrez_uniprot_conflict_genes.add(name)
                        acc_to_drop.add(acc)
            else:
                acc_to_drop.add(acc)

    for iso_acc in isoforms:
        entrez, name = entrez_map[iso_acc]
        if entrez is None:
            canonical = iso_acc.split("-")[0]
            canonical_entry = entrez_map.get(canonical, [False, False])
            if canonical_entry:
                new_entrez, new_name = canonical_entry
                if new_entrez and new_name:
                    entrez_map[iso_acc] = [new_entrez, new_name]
                    continue
            if name:
                new_entrez = name_entrez_dict.get(name, False)
                if new_entrez:
                    entrez_map[iso_acc][0] = new_entrez

                else:
                    gene_name, syn_entrez = synonyms_dict.get(name, [False, False])
                    if syn_entrez:
                        entrez_map[iso_acc] = [syn_entrez, gene_name]
                    else:
                        entrez_uniprot_conflict_genes.add(name)
                        acc_to_drop.add(iso_acc)
            else:
                acc_to_drop.add(iso_acc)

    for acc in acc_to_drop:
        del entrez_map[acc]

    return entrez_map, acc_to_drop, entrez_uniprot_conflict_genes


# ---------------------------------------------------------------------------
# Per-source file parsers
# ---------------------------------------------------------------------------


def _parse_intact_or_biogrid(
    path: str, source_file: str, log_file: str, problem_gene_file: str
):
    if source_file not in ["biogrid", "intact"]:
        raise ValueError(
            f'{source_file} is not a valid source file. Please use "biogid" or "intact"'
        )
    log_file = open(log_file, "a+")
    gene_file = open(problem_gene_file, "a+")
    pair_map: dict[tuple[str, str], _ParsedRow] = {}
    total = 0
    non_human_skipped = 0
    unmapped_uniprot_skipped = 0
    deleted_uniprot_skipped = 0
    method_skipped = 0
    pimd_skipped = 0
    no_gene_map_skipped = 0

    acc_map, uniprot_name_map, gene_map, non_gene_accs, conflict_genes = (
        _load_id_mappings()
    )
    n_deleted = 0
    deleted: set[str] = set()

    if source_file == "intact":
        id_idx_1 = 0
        id_idx_2 = 1

    elif source_file == "biogrid":
        id_idx_1 = 2
        id_idx_2 = 3

    with contextlib.closing(open_mitab(path)) as fh:
        for line in fh:
            total += 1
            both_human = line[9].startswith("taxid:9606") and line[10].startswith(
                "taxid:9606"
            )
            if not both_human:
                non_human_skipped += 1
                continue

            id_values = [line[id_idx_1], line[id_idx_2]]
            acc2 = _parse_uniprot_acc(id_values[1])
            acc1 = _parse_uniprot_acc(id_values[0])
            has_uniprot_acc = all([acc1, acc2])

            if not has_uniprot_acc:
                mapped_str = ", ".join(
                    [id_values[i] for i, a in enumerate([acc1, acc2]) if a is None]
                )
                msg = f"{acc1} - {acc2} was dropped as {mapped_str} isn't valid UNIPROT acc, File: {path}\n"
                log_file.write(msg)
                unmapped_uniprot_skipped += 1
                continue

            iso1 = "-" in acc1
            iso2 = "-" in acc2

            p1 = acc_map.get(acc1)
            p2 = acc_map.get(acc2)

            uniprot_name1 = uniprot_name_map.get(p1)
            uniprot_name2 = uniprot_name_map.get(p2)

            tech, skip = _parse_tech(line[6])
            if skip:
                msg = f"{acc1} - {acc2} was dropped as due to detection method,\t File: {path}\n"
                log_file.write(msg)
                method_skipped += 1
                continue

            if p1 is None or p2 is None:
                deleted_str = ", ".join(
                    [a for a, p in zip([acc1, acc2], [p1, p2]) if p is None]
                )
                msg = f"{acc1} - {acc2} was dropped as {deleted_str} was dropped from UNIPROT,\t File: {path}\n"
                log_file.write(msg)
                deleted_uniprot_skipped += 1
                if p1 is None:
                    deleted.add(acc1)
                elif p2 is None:
                    deleted.add(acc2)
                continue

            pmid, skip = _parse_pmid(line[8])
            if skip:
                msg = f"{acc1}-{acc2} was dropped as {line[8]} could not be mapped to PMID,\t File: {path}\n"
                log_file.write(msg)
                pimd_skipped += 1
                continue

            itype = _parse_interaction_type(line[11])
            link = _parse_links(line[13])
            source = _parse_source(line[12])

            entrez = []
            gene_names = []

            skip = False
            no_gene_map = []
            for protein in [p1, p2]:
                try:
                    gene_id, gene_name = gene_map[protein]
                    entrez.append(gene_id)
                    gene_names.append(gene_name)
                except KeyError:
                    no_gene_map.append(protein)
                    deleted.add(protein)
                    skip = True
                    break
            if skip:
                missing_gene_map = ", ".join(no_gene_map)
                msg = f"{acc1}-{acc2} were dropped as {missing_gene_map} didn't map to any Entrez gene \n"
                log_file.write(msg)
                no_gene_map_skipped += 1
                continue

            key = tuple(sorted([p1, p2]))

            if iso1 or iso2:  # make sure to update the generic interaction as well
                can_p1 = p1.split("-")[0] if iso1 else p1
                can_p2 = p2.split("-")[0] if iso2 else p2
                can_key = tuple(sorted([can_p1, can_p2]))
                if can_key in pair_map:
                    cr = pair_map[can_key]
                    cr.pmids.add(pmid)
                    cr.source.add(source)
                    if tech:
                        cr.techs.add(tech)
                    if itype:
                        cr.types.add(itype)
                    if link:
                        cr.link |= link
                else:
                    pair_map[can_key] = _ParsedRow(
                        p1_id=can_p1,
                        p2_id=can_p2,
                        iso=[False, False],
                        gene_names=gene_names,
                        entrez_ids=entrez,
                        uniprot_names=[
                            uniprot_name_map[can_p1],
                            uniprot_name_map[can_p2],
                        ],
                        pmids={pmid},
                        source={source},
                        techs={tech} if tech else set(),
                        types={itype} if itype else set(),
                        link=link,
                    )

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
    total_skipped = (
        non_human_skipped
        + unmapped_uniprot_skipped
        + deleted_uniprot_skipped
        + method_skipped
        + pimd_skipped
        + no_gene_map_skipped
    )

    def _row(label: str, n: int) -> str:
        return "\t".join([label, str(n), f"{round(n / total * 100, 2)}%"]) + "\n"

    agg_skipped_msg = (
        "Reason\tN-PPI\tpercent\n"
        + _row("Non-human-protein", non_human_skipped)
        + _row("Unmapped-in-uniprot", unmapped_uniprot_skipped)
        + _row("Deleted-from-uniprot", deleted_uniprot_skipped)
        + _row("Skipped-method", method_skipped)
        + _row("No-pubmed-id", pimd_skipped)
        + _row("No-gene-mapped", no_gene_map_skipped)
    )
    log_file.write(agg_skipped_msg + f"File: {path} \n")
    log_file.write(
        f"A total of {len(non_gene_accs)} uniprot accessions didn't map to a gene\n"
    )
    log_file.write(f"A total of {len(conflict_genes)} conflict genes Uniprot/Entrez")

    problem_gene_dict = {
        source_file: {
            "conflict_genes": list(conflict_genes),
            "non_mapped_genes": list(non_gene_accs),
        }
    }
    json.dump(problem_gene_dict, gene_file, indent=2)
    gene_file.write("\n")
    gene_file.close()
    log_file.close()

    return pair_map, total, total_skipped, deleted, n_deleted


# ---------------------------------------------------------------------------
# Gene synonym helpers
# ---------------------------------------------------------------------------


def _load_gene_synonyms(entrez_ids: set[int]) -> dict[int, set[str]]:
    """
    Collect synonym strings for the given Entrez IDs from two sources:
      - Homo_sapiens.gene_info  col 4 (pipe-separated synonyms)
      - HUMAN_9606_idmapping.dat  Gene_Name and Gene_Synonym type rows

    Returns: entrez_id -> set[synonym_string]
    """
    synonyms: dict[int, set[str]] = {eid: set() for eid in entrez_ids}

    with open(data_path("gene_info"), "r") as f:
        for line in f:
            parts = line.strip().split("\t")
            if len(parts) < 5:
                continue
            try:
                eid = int(parts[1])
            except ValueError:
                continue
            if eid not in synonyms:
                continue
            for syn in parts[4].split("|"):
                syn = syn.strip()
                if syn and syn != "-":
                    synonyms[eid].add(syn)

    # Two-pass over idmapping: first collect acc→entrez, then Gene_Name/Gene_Synonym.
    acc_to_entrez: dict[str, int] = {}
    with open(data_path("human_idmapping"), "r") as f:
        for line in f:
            parts = line.strip().split("\t")
            if len(parts) != 3:
                continue
            acc, type_, val = parts
            if type_ == "GeneID":
                try:
                    eid = int(val)
                    if eid in synonyms:
                        acc_to_entrez[acc] = eid
                except ValueError:
                    pass

    with open(data_path("human_idmapping"), "r") as f:
        for line in f:
            parts = line.strip().split("\t")
            if len(parts) != 3:
                continue
            acc, type_, val = parts
            if type_ in ("Gene_Name", "Gene_Synonym"):
                eid = acc_to_entrez.get(acc)
                if eid is not None and val.strip():
                    synonyms[eid].add(val.strip())

    return synonyms


# ---------------------------------------------------------------------------
# upsert interactions
# ---------------------------------------------------------------------------


def _upsert(
    rows: list[_ParsedRow],
) -> tuple[set[int], int, int]:
    """
    Bulk-insert for empty tables. Total DB round-trips: O(tables), not O(rows).

    - transaction.atomic() = one commit instead of one per statement (big on MariaDB)
    - bulk_create without ignore_conflicts = Django sets PKs on returned objects,
      so no re-query needed to build caches
    - M2M through tables flushed in one bulk_create each at the end
    """
    BATCH = 50_000

    with transaction.atomic():
        print("  Collecting entities...", flush=True)

        genes: dict[int, str] = {}  # entrez_id -> gene_name
        canonical_proteins: dict[
            str, tuple[int, str]
        ] = {}  # acc -> (entrez_id, uniprot_name)
        isoform_proteins: dict[
            str, tuple[int, str, str]
        ] = {}  # acc -> (entrez_id, name, canonical_acc)
        exp_types: dict[str, str] = {}  # name -> mi_code
        sources: set[str] = set()
        pmids: set[int] = set()
        itypes: set[str] = set()

        for row in rows:
            for p_acc, p_name, iso_bool, entrez, gene_name in zip(
                [row.p1_id, row.p2_id],
                row.uniprot_names,
                row.iso,
                row.entrez_ids,
                row.gene_names,
            ):
                eid = int(entrez)
                genes[eid] = gene_name or ""
                if iso_bool:
                    can = p_acc.split("-")[0]
                    canonical_proteins[can] = (eid, p_name or "")
                    isoform_proteins[p_acc] = (eid, p_name or "", can)
                else:
                    canonical_proteins[p_acc] = (eid, p_name)
            for s in row.source:
                sources.add(s)
            for p in row.pmids:
                if p is not None:
                    pmids.add(p)
            for mi, tech in row.techs:
                exp_types[tech] = mi
            for t in row.types:
                itypes.add(t)
            for db, _ in row.link:
                sources.add(db)

        print("  Checking existing records...", flush=True)
        existing_genes: dict[int, Gene] = {
            g.entrez_id: g for g in Gene.objects.filter(entrez_id__in=genes)
        }

        existing_proteins: dict[str, Protein] = {
            p.uniprot_accession: p
            for p in Protein.objects.filter(
                uniprot_accession__in=list(canonical_proteins) + list(isoform_proteins)
            )
        }
        # Deduplicate sources by lowercase name; keep first-seen original casing
        sources_lc: dict[str, str] = {}
        for s in sources:
            if s.lower() not in sources_lc:
                sources_lc[s.lower()] = s

        existing_sources: dict[str, Source] = {
            s.name_lower: s
            for s in Source.objects.annotate(name_lower=Lower("name")).filter(
                name_lower__in=sources_lc.keys()
            )
        }
        existing_pubs: dict[int, Publication] = {
            p.pmid: p for p in Publication.objects.filter(pmid__in=pmids)
        }
        existing_exps: dict[str, ExperimentType] = {
            e.name: e for e in ExperimentType.objects.filter(name__in=exp_types)
        }
        existing_itypes: dict[str, InteractionType] = {
            it.name: it for it in InteractionType.objects.filter(name__in=itypes)
        }

        # ---------------------------------------------------------- #
        # Phase 2: bulk_create only missing rows; merge with existing
        # ---------------------------------------------------------- #
        print("  Inserting genes...", flush=True)
        new_genes = Gene.objects.bulk_create(
            [
                Gene(entrez_id=eid, entrez_name=name)
                for eid, name in genes.items()
                if eid not in existing_genes
            ],
            batch_size=BATCH,
        )
        gene_cache: dict[int, Gene] = {
            **existing_genes,
            **{g.entrez_id: g for g in new_genes},
        }

        print("  Inserting gene synonyms...", flush=True)
        gene_synonym_map = _load_gene_synonyms(set(gene_cache.keys()))
        pending_synonyms: list[GeneSynonym] = []
        for eid, syns in gene_synonym_map.items():
            gene_obj = gene_cache.get(eid)
            if gene_obj is None:
                continue
            for syn in syns:
                pending_synonyms.append(GeneSynonym(gene=gene_obj, synonym=syn))
        GeneSynonym.objects.bulk_create(
            pending_synonyms, batch_size=BATCH, ignore_conflicts=True
        )

        print("  Inserting canonical proteins...", flush=True)
        new_proteins = Protein.objects.bulk_create(
            [
                Protein(uniprot_accession=acc, gene=gene_cache[eid], uniprot_name=name)
                for acc, (eid, name) in canonical_proteins.items()
                if acc not in existing_proteins and eid in gene_cache
            ],
            batch_size=BATCH,
        )
        protein_cache: dict[str, Protein] = {
            **existing_proteins,
            **{p.uniprot_accession: p for p in new_proteins},
        }

        print("  Inserting isoforms...", flush=True)
        for acc, (eid, name, can) in isoform_proteins.items():
            if acc in existing_proteins:
                protein_cache[acc] = existing_proteins[acc]
                continue
            if can not in protein_cache or eid not in gene_cache:
                continue
            iso = Isoform.objects.create(
                uniprot_accession=acc,
                general_protein=protein_cache[can],
                gene=gene_cache[eid],
                uniprot_name=name,
            )
            protein_cache[acc] = iso

        print("  Inserting sources...", flush=True)
        new_sources = Source.objects.bulk_create(
            [
                Source(name=original)
                for lc, original in sources_lc.items()
                if lc not in existing_sources
            ],
            batch_size=BATCH,
        )
        # Keyed by lowercase name for case-insensitive lookups
        source_cache: dict[str, Source] = {
            **existing_sources,
            **{s.name.lower(): s for s in new_sources},
        }

        print("  Inserting publications...", flush=True)
        new_pubs = Publication.objects.bulk_create(
            [Publication(pmid=p) for p in pmids if p not in existing_pubs],
            batch_size=BATCH,
        )
        pub_cache: dict[int, Publication] = {
            **existing_pubs,
            **{p.pmid: p for p in new_pubs},
        }

        print("  Checking for overlapping PSI_MI codes...", flush=True)
        existing_mi_map: dict[str, ExperimentType] = {
            et.psi_mi_code: et for et in ExperimentType.objects.exclude(psi_mi_code="")
        }
        for n, mi in exp_types.items():
            if n not in existing_exps and mi != "" and mi in existing_mi_map:
                existing = existing_mi_map[mi]
                print(
                    f"    Warning: experiment type '{n}' has PSI-MI code '{mi}'",
                    flush=True,
                )
                print(
                    f"    which overlaps with existing type '{existing.name}'",
                    flush=True,
                )

        print("  Inserting experiment types...", flush=True)
        new_exps = ExperimentType.objects.bulk_create(
            [
                ExperimentType(name=n, psi_mi_code=mi, quality_score=0.0)
                for n, mi in exp_types.items()
                if n not in existing_exps
            ],
            batch_size=BATCH,
        )
        exp_cache: dict[str, ExperimentType] = {
            **existing_exps,
            **{e.name: e for e in new_exps},
        }

        print("  Inserting interaction types...", flush=True)
        new_itypes = InteractionType.objects.bulk_create(
            [
                InteractionType(name=n, psi_mi_code="")
                for n in itypes
                if n not in existing_itypes
            ],
            batch_size=BATCH,
        )
        itype_cache: dict[str, InteractionType] = {
            **existing_itypes,
            **{it.name: it for it in new_itypes},
        }

        # ---------------------------------------------------------- #
        # Phase 3: resolve protein pairs, insert Interactions
        # ---------------------------------------------------------- #
        print("  Building interactions...", flush=True)

        # Group rows by their sorted (pid1, pid2) so M2M data for the same
        # interaction pair (from different input lines) is merged correctly.
        pair_map: dict[tuple[int, int], list[_ParsedRow]] = {}
        for row in rows:
            p1 = protein_cache.get(row.p1_id)
            p2 = protein_cache.get(row.p2_id)
            if p1 is None or p2 is None:
                continue
            pair = (min(p1.pk, p2.pk), max(p1.pk, p2.pk))
            pair_map.setdefault(pair, []).append(row)

        print("  Inserting interactions...", flush=True)
        existing_interactions: dict[tuple[int, int], Interaction] = {
            (ia.protein_1_id, ia.protein_2_id): ia
            for ia in Interaction.objects.filter(
                protein_1_id__in={p for p, _ in pair_map},
                protein_2_id__in={p for _, p in pair_map},
            )
            if (ia.protein_1_id, ia.protein_2_id) in pair_map
        }
        new_interaction_objs = Interaction.objects.bulk_create(
            [
                Interaction(protein_1_id=pid1, protein_2_id=pid2, score=0.0)
                for pid1, pid2 in pair_map
                if (pid1, pid2) not in existing_interactions
            ],
            batch_size=BATCH,
        )
        interaction_cache: dict[tuple[int, int], Interaction] = {
            **existing_interactions,
            **{(ia.protein_1_id, ia.protein_2_id): ia for ia in new_interaction_objs},
        }
        touched = {ia.pk for ia in interaction_cache.values()}

    # ---------------------------------------------------------- #
    # Phase 4: collect M2M rows and flush in a separate transaction
    # (entities + interactions already committed above, so MariaDB
    #  doesn't hold a single giant undo log through the M2M inserts)
    # ---------------------------------------------------------- #
    print("  Building M2M relations...", flush=True)

    SourceThrough = Interaction.sources.through
    PubThrough = Interaction.publications.through
    ExpThrough = Interaction.experiments.through
    ItypeThrough = Interaction.interaction_types.through

    seen_sources: set[tuple[int, int]] = set()
    seen_pubs: set[tuple[int, int]] = set()
    seen_exps: set[tuple[int, int]] = set()
    seen_itypes: set[tuple[int, int]] = set()
    seen_xrefs: set[tuple[int, str, int]] = set()

    pending_sources: list = []
    pending_pubs: list = []
    pending_exps: list = []
    pending_itypes: list = []
    pending_xrefs: list[InteractionCrossReference] = []

    for (pid1, pid2), pair_rows in pair_map.items():
        ia = interaction_cache.get((pid1, pid2))
        if ia is None:
            continue
        iid = ia.pk
        for row in pair_rows:
            for s in row.source:
                sl = s.lower()
                if (
                    sl in source_cache
                    and (iid, source_cache[sl].pk) not in seen_sources
                ):
                    seen_sources.add((iid, source_cache[sl].pk))
                    pending_sources.append(
                        SourceThrough(interaction_id=iid, source_id=source_cache[sl].pk)
                    )
            for p in row.pmids:
                if p in pub_cache and (iid, pub_cache[p].pk) not in seen_pubs:
                    seen_pubs.add((iid, pub_cache[p].pk))
                    pending_pubs.append(
                        PubThrough(interaction_id=iid, publication_id=pub_cache[p].pk)
                    )
            for mi, tech in row.techs:
                if tech in exp_cache and (iid, exp_cache[tech].pk) not in seen_exps:
                    seen_exps.add((iid, exp_cache[tech].pk))
                    pending_exps.append(
                        ExpThrough(
                            interaction_id=iid, experimenttype_id=exp_cache[tech].pk
                        )
                    )
            for t in row.types:
                if t in itype_cache and (iid, itype_cache[t].pk) not in seen_itypes:
                    seen_itypes.add((iid, itype_cache[t].pk))
                    pending_itypes.append(
                        ItypeThrough(
                            interaction_id=iid, interactiontype_id=itype_cache[t].pk
                        )
                    )
            for db, link_id in row.link:
                dbl = db.lower()
                if (
                    dbl in source_cache
                    and (iid, link_id, source_cache[dbl].pk) not in seen_xrefs
                ):
                    seen_xrefs.add((iid, link_id, source_cache[dbl].pk))
                    pending_xrefs.append(
                        InteractionCrossReference(
                            interaction_id=iid,
                            link=link_id,
                            source=source_cache[dbl],
                            species=None,
                        )
                    )

    with transaction.atomic():
        print(f"  Flushing sources ({len(pending_sources):,})...", flush=True)
        SourceThrough.objects.bulk_create(
            pending_sources, batch_size=BATCH, ignore_conflicts=True
        )
        print(f"  Flushing publications ({len(pending_pubs):,})...", flush=True)
        PubThrough.objects.bulk_create(
            pending_pubs, batch_size=BATCH, ignore_conflicts=True
        )
        print(f"  Flushing experiment types ({len(pending_exps):,})...", flush=True)
        ExpThrough.objects.bulk_create(
            pending_exps, batch_size=BATCH, ignore_conflicts=True
        )
        print(f"  Flushing interaction types ({len(pending_itypes):,})...", flush=True)
        ItypeThrough.objects.bulk_create(
            pending_itypes, batch_size=BATCH, ignore_conflicts=True
        )
        print(f"  Flushing cross-references ({len(pending_xrefs):,})...", flush=True)
        InteractionCrossReference.objects.bulk_create(
            pending_xrefs, batch_size=BATCH, ignore_conflicts=True
        )

    new_protein_entries = len(canonical_proteins) + len(isoform_proteins)
    return touched, new_protein_entries, len(pair_map)


# ---------------------------------------------------------------------------
# Secondary UniProt accessions
# ---------------------------------------------------------------------------


def _refresh_secondary_accessions() -> None:
    """
    Populate ProteinSynonym rows for secondary UniProt accessions.

    sec_ac.txt maps secondary → primary accessions (all organisms).
    We only add entries for primaries already present in the DB.
    """
    secondary_to_primary: dict[str, str] = {}
    past_header = False
    with open(data_path("sec_ac"), "r") as f:
        for line in f:
            if line.startswith("_"):
                past_header = True
                continue
            if not past_header:
                continue
            parts = line.strip().split()
            if len(parts) >= 2:
                secondary_to_primary[parts[0]] = parts[-1]

    primary_accs = set(secondary_to_primary.values())
    protein_by_acc: dict[str, int] = {
        acc: pk
        for acc, pk in Protein.objects.values_list("uniprot_accession", "pk")
        if acc in primary_accs
    }

    pending: list[ProteinSynonym] = [
        ProteinSynonym(
            protein_id=protein_by_acc[primary],
            synonym=secondary,
            additional_information="secondary_uniprot_accession",
        )
        for secondary, primary in secondary_to_primary.items()
        if primary in protein_by_acc
    ]
    ProteinSynonym.objects.bulk_create(
        pending, batch_size=50_000, ignore_conflicts=True
    )


# ---------------------------------------------------------------------------
# Source interaction count
# ---------------------------------------------------------------------------


def _recompute_source_interaction_counts() -> None:
    """Set Source.n_connected_interactions to the count of linked Interactions."""
    from django.db.models import Count

    counts: dict[int, int] = {
        row["pk"]: row["cnt"]
        for row in Source.objects.annotate(cnt=Count("interactions")).values(
            "pk", "cnt"
        )
    }
    objs = [Source(pk=pk, n_connected_interactions=cnt) for pk, cnt in counts.items()]
    Source.objects.bulk_update(objs, ["n_connected_interactions"], batch_size=1_000)


# ---------------------------------------------------------------------------
# Source homepage URLs
# ---------------------------------------------------------------------------


def _assign_source_urls() -> None:
    """
    Backfill Source.url from the known-homepage registry.

    Idempotent and run on every invocation: fills only sources whose url is
    still blank, so manually-edited URLs (e.g. via the admin) are preserved.
    Covers both freshly imported sources and pre-existing rows from earlier
    runs that predate URL assignment.
    """
    from hippie_website.source_links import homepage_url

    objs: list[Source] = []
    for source in Source.objects.filter(url=""):
        url = homepage_url(source.name)
        if url:
            source.url = url
            objs.append(source)
    if objs:
        Source.objects.bulk_update(objs, ["url"], batch_size=1_000)


# ---------------------------------------------------------------------------
# Rescoring
# ---------------------------------------------------------------------------


def _bulk_update_scores(objs: list[Interaction]) -> None:
    """
    Write scores back to the DB.

    MariaDB path: stage into a temp table then do a single JOIN UPDATE.
    bulk_update generates one CASE WHEN clause per row per batch, which
    MariaDB parses slowly at scale (several minutes for 750k+ rows).
    A temp-table JOIN UPDATE reduces that to two queries regardless of size.

    SQLite path (dev only): plain bulk_update with small batches.
    """
    if not objs:
        return

    if connection.vendor == "mysql":
        table = Interaction._meta.db_table
        STAGE_BATCH = 50_000
        with connection.cursor() as cur:
            cur.execute(
                "CREATE TEMPORARY TABLE _score_update "
                "(id INT PRIMARY KEY, score DOUBLE NOT NULL)"
            )
            pairs = [(o.pk, o.score) for o in objs]
            for i in range(0, len(pairs), STAGE_BATCH):
                chunk = pairs[i : i + STAGE_BATCH]
                cur.executemany("INSERT INTO _score_update VALUES (%s, %s)", chunk)
                print(
                    f"    staged {min(i + STAGE_BATCH, len(pairs)):,}/{len(pairs):,}",
                    flush=True,
                )
            print("  Applying scores via JOIN UPDATE...", flush=True)
            cur.execute(
                f"UPDATE `{table}` i "
                "JOIN _score_update t ON i.id = t.id "
                "SET i.score = t.score"
            )
            cur.execute("DROP TEMPORARY TABLE _score_update")
    else:
        Interaction.objects.bulk_update(objs, ["score"], batch_size=1_000)


def _rescore_all() -> None:
    """Recompute and persist scores for all Interactions in the DB."""
    # Query 1a: protein_pk -> effective gene_pk.
    # Canonical proteins supply their own gene_id; isoforms override with the canonical's gene.
    protein_to_gene: dict[int, int] = dict(Protein.objects.values_list("pk", "gene_id"))
    for iso_pk, can_gene_id in Isoform.objects.values_list(
        "protein_ptr_id", "general_protein__gene_id"
    ):
        protein_to_gene[iso_pk] = can_gene_id

    # Query 1b: interaction_pk -> sorted (gene_pk_1, gene_pk_2) pair.
    # values_list returns integer tuples — no ORM object overhead.
    ipk_to_gene: dict[int, tuple[int, int]] = {}
    for ipk, p1_pk, p2_pk in Interaction.objects.values_list(
        "pk", "protein_1_id", "protein_2_id"
    ):
        g1 = protein_to_gene.get(p1_pk)
        g2 = protein_to_gene.get(p2_pk)
        if g1 is not None and g2 is not None:
            ipk_to_gene[ipk] = (min(g1, g2), max(g1, g2))

    # Accumulators: gene pair → sets of distinct FK pks
    gene_pubs: dict[tuple[int, int], set[int]] = defaultdict(set)
    gene_species: dict[tuple[int, int], set[int]] = defaultdict(set)
    gene_exptypes: dict[tuple[int, int], set[int]] = defaultdict(set)

    # Query 2: bulk-load interaction → publication join table
    for ia_pk, pub_pk in Interaction.publications.through.objects.values_list(
        "interaction_id", "publication_id"
    ):
        if gp := ipk_to_gene.get(ia_pk):
            gene_pubs[gp].add(pub_pk)

    # Query 3: gene pair → ortholog species from OrthologInteraction (gene-based)
    oi_gene_pairs: dict[int, tuple[int, int]] = {
        oi_pk: (g1, g2)
        for oi_pk, g1, g2 in OrthologInteraction.objects.values_list(
            "pk", "gene_1_id", "gene_2_id"
        )
    }
    for (
        oi_pk,
        sp_pk,
    ) in OrthologInteraction.ortholog_species.through.objects.values_list(
        "orthologinteraction_id", "species_id"
    ):
        if gp := oi_gene_pairs.get(oi_pk):
            gene_species[gp].add(sp_pk)

    # Query 4: bulk-load interaction → experiment type join table
    for ia_pk, et_pk in Interaction.experiments.through.objects.values_list(
        "interaction_id", "experimenttype_id"
    ):
        if gp := ipk_to_gene.get(ia_pk):
            gene_exptypes[gp].add(et_pk)

    # Query 5: experiment type quality scores
    et_quality: dict[int, float] = {
        pk: (score or 0.0)
        for pk, score in ExperimentType.objects.values_list("pk", "quality_score")
    }

    # Score each interaction in Python — no further DB queries
    total = len(ipk_to_gene)
    objs = []
    for i, (ipk, gp) in enumerate(ipk_to_gene.items()):
        if i % 100_000 == 0:
            print(f"  Scoring {i:,}/{total:,}...", flush=True)
        pub_n = len(gene_pubs.get(gp, ()))
        orth_n = len(gene_species.get(gp, ()))
        exp_sum = sum(et_quality.get(et_pk, 0.0) for et_pk in gene_exptypes.get(gp, ()))
        objs.append(Interaction(pk=ipk, score=_compute_score(pub_n, orth_n, exp_sum)))

    print(f"  Writing scores ({len(objs):,} interactions)...", flush=True)
    _bulk_update_scores(objs)


# ---------------------------------------------------------------------------
# Resource version labels (data/sources.json key -> human-readable label)
# ---------------------------------------------------------------------------

_RESOURCE_LABELS: dict[str, str] = {
    "sec_ac": "UniProt (sec_ac)",
    "human_idmapping": "UniProt (idmapping)",
    "gene_info": "NCBI Gene",
    "intact_human": "IntAct (human)",
    "intact_full": "IntAct (full)",
    "biogrid": "BioGRID",
    "gtex_sample_attrs": "GTEx (sample attributes)",
    "gtex_gene_reads": "GTEx (gene reads)",
    "orthology_alliance": "Alliance of Genome Resources",
    "wormbase": "WormBase",
    "rgd": "RGD",
    "mgi": "MGI",
    "zfin": "ZFIN",
    "xenbase_tropicalis": "Xenbase (X. tropicalis)",
    "xenbase_laevis": "Xenbase (X. laevis)",
    "flybase": "FlyBase",
    "sgd": "SGD",
    "uniprot_to_orthology": "UniProt → Orthology map",
    "techniques_scoring": "Technique scoring",
    "pod_flat": "Negative Data",
}


def _current_resource_versions() -> dict[str, str]:
    """Human-readable resource label -> version, read fresh from data/sources.json.

    sources.json is the source of truth: its 'version' fields are updated by
    download_update_data.sh / record_fetch after the data files themselves are
    updated, so this always reflects what's actually sitting in data/.
    """
    return {
        _RESOURCE_LABELS.get(key, key): entry["version"]
        for key, entry in load_sources().items()
        if entry.get("version")
    }


# ---------------------------------------------------------------------------
# Ensembl ID population (ENSG on Gene, ENST/ENSP on Isoform)
#
# Hybrid: parse the already-downloaded UniProt HUMAN_9606_idmapping.dat first
# (instant, no network), then fall back to the Ensembl REST API only for the
# genes/isoforms whose relevant list is still empty. IDs are stored unversioned
# (DIGGER URLs need bare ENSG/ENST/ENSP). Used to build DIGGER cross-links.
# ---------------------------------------------------------------------------

_ENSEMBL_REST = "https://rest.ensembl.org"
_ENSEMBL_MIN_INTERVAL = 1.0 / 15  # documented Ensembl rate limit: ~15 req/s
_ENSEMBL_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "hippie-ensembl/1.0",
}


def _strip_version(ensembl_id: str) -> str:
    """Drop the ``.N`` version suffix (ENSG00000141510.14 -> ENSG00000141510)."""
    return ensembl_id.split(".", 1)[0]


def _dedupe(ids: list[str]) -> list[str]:
    """Order-preserving de-dupe, dropping empties."""
    return list(dict.fromkeys(i for i in ids if i))


def _parse_idmapping_ensembl() -> tuple[
    dict[str, list[str]], dict[str, list[str]], dict[str, list[str]]
]:
    """Single pass over HUMAN_9606_idmapping.dat collecting unversioned Ensembl IDs.

    Returns three accession-keyed maps:
        acc_ensg — canonical accession -> [ENSG, ...]   (from ``Ensembl``)
        iso_enst — isoform accession   -> [ENST, ...]   (from ``Ensembl_TRS``)
        iso_ensp — isoform accession   -> [ENSP, ...]   (from ``Ensembl_PRO``)

    Isoform (``ACC-N``) rows never carry an ``Ensembl`` gene row, so canonical
    ENSGs come only from no-dash accessions. TRS/PRO rows also exist for
    canonical accessions but are irrelevant here, so we keep only the dash ones.
    """
    acc_ensg: dict[str, list[str]] = defaultdict(list)
    iso_enst: dict[str, list[str]] = defaultdict(list)
    iso_ensp: dict[str, list[str]] = defaultdict(list)
    with open(data_path("human_idmapping"), "r") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 3:
                continue
            acc, id_type, value = parts[0], parts[1], parts[2]
            if id_type == "Ensembl":
                acc_ensg[acc].append(_strip_version(value))
            elif id_type == "Ensembl_TRS" and "-" in acc:
                iso_enst[acc].append(_strip_version(value))
            elif id_type == "Ensembl_PRO" and "-" in acc:
                iso_ensp[acc].append(_strip_version(value))
    return acc_ensg, iso_enst, iso_ensp


def _ensembl_json(
    path: str, *, payload: dict | None = None, throttle: list[float]
) -> object | None:
    """Rate-limited Ensembl REST call (GET, or POST when ``payload`` given).

    ``throttle`` is a one-element list holding the last request's monotonic time
    so successive calls stay under ~15 req/s. Honours HTTP 429 + Retry-After.
    Returns parsed JSON, or None on a 4xx / exhausted retries.
    """
    url = f"{_ENSEMBL_REST}{path}"
    data = None
    headers = dict(_ENSEMBL_HEADERS)
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    for attempt in range(5):
        wait = throttle[0] + _ENSEMBL_MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        throttle[0] = time.monotonic()
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry_after = e.headers.get("Retry-After")
                time.sleep(float(retry_after) if retry_after else 1.0)
                continue
            if 400 <= e.code < 500:
                return None  # unknown symbol/accession — no cross-reference
            time.sleep(1.0 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, ValueError):
            time.sleep(1.0 * (attempt + 1))
    return None


def _fill_isoforms_from_ensembl(stdout, isoforms, gene_by_pk, touched_genes) -> None:
    """xrefs lookup for isoforms missing ENST or ENSP; also back-fills gene ENSG."""
    needing = [iso for iso in isoforms if not iso.enst or not iso.ensp]
    n = len(needing)
    if not n:
        return
    stdout.write(f"  Ensembl API: resolving {n} isoform(s) (~15 req/s)...")
    throttle = [0.0]
    updated: list = []
    for i, iso in enumerate(needing, 1):
        acc = iso.uniprot_accession
        data = _ensembl_json(
            f"/xrefs/symbol/homo_sapiens/{acc}?content-type=application/json",
            throttle=throttle,
        )
        if isinstance(data, list):
            for xref in data:
                xid = _strip_version(xref.get("id", ""))
                xtype = xref.get("type")
                if not xid:
                    continue
                if xtype == "transcript":
                    iso.enst = _dedupe([*iso.enst, xid])
                elif xtype == "translation":
                    iso.ensp = _dedupe([*iso.ensp, xid])
                elif xtype == "gene":
                    gene = gene_by_pk.get(iso.gene_id)
                    if gene is not None:
                        merged = _dedupe([*gene.ensg, xid])
                        if merged != gene.ensg:
                            gene.ensg = merged
                            touched_genes.add(gene.pk)
            updated.append(iso)
        if i % 25 == 0 or i == n:
            stdout.write(f"\r    isoforms {i}/{n}", ending="")
    stdout.write("")  # newline after the progress line
    if updated:
        from hippie_website.models import Isoform

        Isoform.objects.bulk_update(updated, ["enst", "ensp"], batch_size=5000)
    stdout.write(f"    Ensembl API resolved {len(updated)} isoform(s).")


def _fill_genes_from_ensembl(stdout, genes, touched_genes) -> None:
    """Batch symbol lookup for genes still missing an ENSG after Phase 1 + isoforms."""
    empties = [g for g in genes if not g.ensg and g.entrez_name]
    if not empties:
        return
    by_symbol: dict[str, list] = defaultdict(list)
    for g in empties:
        by_symbol[g.entrez_name].append(g)
    symbols = list(by_symbol)
    stdout.write(f"  Ensembl API: resolving ENSG for {len(symbols)} gene symbol(s)...")
    throttle = [0.0]
    resolved = 0
    for start in range(0, len(symbols), 500):
        chunk = symbols[start : start + 500]
        data = _ensembl_json(
            "/lookup/symbol/homo_sapiens",
            payload={"symbols": chunk},
            throttle=throttle,
        )
        if not isinstance(data, dict):
            continue
        for symbol, entry in data.items():
            ensg = (
                _strip_version(entry.get("id", "")) if isinstance(entry, dict) else ""
            )
            if not ensg:
                continue
            for g in by_symbol.get(symbol, []):
                merged = _dedupe([*g.ensg, ensg])
                if merged != g.ensg:
                    g.ensg = merged
                    touched_genes.add(g.pk)
                    resolved += 1
    stdout.write(f"    Ensembl API resolved {resolved} gene(s).")


def _populate_ensembl_ids(stdout, *, skip_api: bool = False) -> None:
    """Fill ``Gene.ensg`` / ``Isoform.enst`` / ``Isoform.ensp`` (local then API)."""
    from hippie_website.models import Gene, Isoform, Protein

    stdout.write("  Parsing Ensembl IDs from idmapping.dat...")
    acc_ensg, iso_enst, iso_ensp = _parse_idmapping_ensembl()

    # Genes: union ENSGs from each gene's canonical protein accessions.
    gene_ensg: dict[int, list[str]] = defaultdict(list)
    canon = Protein.objects.filter(isoform__isnull=True).values_list(
        "uniprot_accession", "gene_id"
    )
    for acc, gene_id in canon.iterator():
        if acc in acc_ensg:
            gene_ensg[gene_id].extend(acc_ensg[acc])

    genes = list(Gene.objects.all())
    for g in genes:
        g.ensg = _dedupe(gene_ensg.get(g.pk, []))
    Gene.objects.bulk_update(genes, ["ensg"], batch_size=5000)

    # Isoforms: ENST/ENSP keyed by the isoform's own accession.
    isoforms = list(Isoform.objects.all())
    for iso in isoforms:
        iso.enst = _dedupe(iso_enst.get(iso.uniprot_accession, []))
        iso.ensp = _dedupe(iso_ensp.get(iso.uniprot_accession, []))
    Isoform.objects.bulk_update(isoforms, ["enst", "ensp"], batch_size=5000)

    stdout.write(
        f"    Local: {sum(1 for g in genes if g.ensg)}/{len(genes)} genes, "
        f"{sum(1 for i in isoforms if i.enst or i.ensp)}/{len(isoforms)} isoforms."
    )

    if skip_api:
        stdout.write("  Skipping Ensembl API fallback (--skip-ensembl-api).")
        return

    gene_by_pk = {g.pk: g for g in genes}
    touched_genes: set[int] = set()
    # Isoforms first — their xrefs also back-fill some gene ENSGs.
    _fill_isoforms_from_ensembl(stdout, isoforms, gene_by_pk, touched_genes)
    _fill_genes_from_ensembl(stdout, genes, touched_genes)
    if touched_genes:
        Gene.objects.bulk_update(
            [gene_by_pk[pk] for pk in touched_genes], ["ensg"], batch_size=5000
        )


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
            "--skip-ensembl-api",
            action="store_true",
            help="Populate Ensembl IDs from the local idmapping file only "
            "(skip the Ensembl REST fallback for still-empty rows)",
        )

    def handle(self, *_args: object, **options: object) -> None:
        biogrid_path: str | None = options["biogrid"]  # type: ignore[assignment]
        intact_path: str | None = options["intact"]  # type: ignore[assignment]
        rescore_all: bool = options["rescore_all"]  # type: ignore[assignment]
        skip_ensembl_api: bool = options["skip_ensembl_api"]  # type: ignore[assignment]

        if biogrid_path or intact_path:
            touched_ids: set[int] = set()
            log_file_path = f"logs/update_hippie_{datetime.now().date()}.log"
            problem_gene_file_path = (
                f"logs/update_hippie_problem_genes_{datetime.now().date()}.json"
            )
            Path(log_file_path).parent.mkdir(parents=True, exist_ok=True)

            if biogrid_path:
                self.stdout.write(f"Parsing BioGRID: {biogrid_path}")
                pair_map, total, skipped, _, _ = _parse_intact_or_biogrid(
                    biogrid_path, "biogrid", log_file_path, problem_gene_file_path
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
                    intact_path, "intact", log_file_path, problem_gene_file_path
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
        if rescore_all:
            self.stdout.write("Rescoring interactions.")
            _rescore_all()

        from django.core.management import call_command

        self.stdout.write("Refreshing denormalised protein stats.")
        call_command("recompute_protein_stats")
        self.stdout.write("Refreshing interaction isoform flags.")
        call_command("recompute_interaction_flags")  # also bumps the browse cache epoch
        self.stdout.write("Recomputing source interaction counts.")
        _recompute_source_interaction_counts()
        self.stdout.write("Assigning source homepage URLs.")
        _assign_source_urls()
        self.stdout.write("Refreshing secondary UniProt accessions.")
        _refresh_secondary_accessions()

        self.stdout.write("Populating Ensembl IDs (ENSG / ENST / ENSP).")
        _populate_ensembl_ids(self.stdout, skip_api=skip_ensembl_api)

        self.stdout.write("Recording release metadata.")
        int_scores = list(Interaction.objects.values_list("score", flat=True))
        nonint_scores = list(NonInteraction.objects.values_list("score", flat=True))
        rel = ReleaseMeta.current() or ReleaseMeta.objects.create()
        rel.set_quartiles("int", compute_quartiles(int_scores))
        rel.set_quartiles("both", compute_quartiles(int_scores + nonint_scores))
        rel.resource_versions = _current_resource_versions()
        rel.save()
        self.stdout.write("Done.")
