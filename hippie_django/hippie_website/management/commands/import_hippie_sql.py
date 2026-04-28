"""
Management command: import_hippie_sql

Imports data from a HIPPIE v2 MariaDB SQL dump into Django models.

Usage:
    python manage.py import_hippie_sql <sql_file>
    python manage.py import_hippie_sql <sql_file> --batch-size 2000
    python manage.py import_hippie_sql <sql_file> --log-file import.log
    python manage.py import_hippie_sql <sql_file> --dry-run

Data-loss / alteration log (`--log-file`, one row per lost value).
Categories:

  - orphan_protein      Junction / interaction row pointing to a protein_id
                        absent from the protein table after dedup — dropped.
  - orphan_lookup       Junction row pointing to a lookup id (source_id,
                        tissue_id, experiment_id, ...) absent from the
                        corresponding master table — dropped.
  - column_discarded    protein2uniprot.uniprot_db_id has no home in the new
                        ProteinUniProt schema; column dropped (one summary row).
  - null_score          Interaction.score was NULL in source; imported as 0.0.
  - effect_superseded   interaction2effect had multiple rows for the same
                        interaction_id; last-row-wins. Superseded values logged.
  - species_unmatched   interaction2link.species text did not prefix-match any
                        Species.name; cross_reference species FK set NULL.
  - ortho_unresolved    OrthologInteraction row not found after bulk_create;
                        attached species rows skipped.
  - bait_prey_error     BaitPreyAssociation.get_or_create raised for a row.
  - interaction_merged  After protein dedup two interactions collapsed onto the
                        same canonical (p1, p2) pair; the non-kept row is
                        logged so the discarded id / score can be inspected.
  - placeholder_exp_type
                        BaitPreyTest rows share a single placeholder
                        ExperimentType "Unknown (legacy import)" — no method
                        info survives from source. One summary row logged.

Non-lossy decisions (not logged per-row):
  - Canonical order enforced (protein_1_id ≤ protein_2_id). kegg_direction
    inverted for swapped rows.
  - source / protein / junction dedup by natural key.
  - interaction2homomint_link skipped (byte-identical subset of
    interaction2link where source_id=6).
  - aggregated_interactions, interaction_id_mapped,
    uniprot2interaction_amount, all tmp_* tables skipped (derived /
    denormalised).
  - NonInteraction, Isoform left empty (no source table in dump).
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterator

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction


# ---------------------------------------------------------------------------
# Tables we actually need from the dump
# ---------------------------------------------------------------------------

WANTED_TABLES: frozenset[str] = frozenset(
    {
        "GO_slim_term",
        "GO_slim_term2term",
        "bait_prey_assoc",
        "experiment_type",
        "homomint_interaction",
        "homomint_interaction2species",
        "i2d_interaction",
        "i2d_interaction2species",
        "interaction",
        "interaction2GO",
        "interaction2effect",
        "interaction2experiment",
        "interaction2keggDirection",
        "interaction2link",
        "interaction2mesh",
        "interaction2pubmed",
        "interaction2source",
        "interaction2species",
        "interaction2type",
        "interaction_type",
        "mesh_term",
        "ortho_interaction",
        "ortho_interaction2species",
        "protein",
        "protein2entrez",
        "protein2tissue",
        "protein2uniprot",
        "source",
        "sp_analysis_end_nodes",
        "species",
        "tissue",
        "uniprot_accession2id",
    }
)

# ---------------------------------------------------------------------------
# SQL value tokeniser
# ---------------------------------------------------------------------------

_ESCAPE: dict[str, str] = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "'": "'",
    "\\": "\\",
    '"': '"',
    "0": "\0",
    "Z": "\x1a",
}
_INSERT_RE = re.compile(r"^INSERT INTO `([^`]+)` VALUES(.*)")


def _tokenize(s: str) -> list[Any]:
    """
    Parse the comma-separated content inside a single MySQL VALUES tuple.
    Handles quoted strings (backslash escapes), NULL, integers, floats.
    """
    tokens: list[Any] = []
    i, n = 0, len(s)

    while i < n:
        # skip whitespace
        while i < n and s[i] in " \t":
            i += 1
        if i >= n:
            break

        if s[i] == "'":
            # quoted string
            i += 1
            buf: list[str] = []
            while i < n:
                c = s[i]
                if c == "\\" and i + 1 < n:
                    buf.append(_ESCAPE.get(s[i + 1], s[i + 1]))
                    i += 2
                elif c == "'":
                    i += 1
                    break
                else:
                    buf.append(c)
                    i += 1
            tokens.append("".join(buf))

        elif s[i : i + 4] == "NULL":
            tokens.append(None)
            i += 4

        else:
            j = i
            while i < n and s[i] != ",":
                i += 1
            raw = s[j:i].strip()
            tokens.append(float(raw) if "." in raw else int(raw))

        # skip separator
        while i < n and s[i] in " \t":
            i += 1
        if i < n and s[i] == ",":
            i += 1

    return tokens


def _parse_row(line: str) -> tuple | None:
    """Parse one VALUES row line '(v1, v2, ...)[,|;]' → tuple, else None."""
    line = line.strip().rstrip(";").rstrip(",").strip()
    if line.startswith("(") and line.endswith(")"):
        return tuple(_tokenize(line[1:-1]))
    return None


# ---------------------------------------------------------------------------
# Streaming dump parser
# ---------------------------------------------------------------------------


def _deduplicate_sources(data: dict[str, list[tuple]]) -> dict[int, int]:
    """
    Merge source rows with the same name into one (keep lowest id).
    Returns {duplicate_old_id: canonical_id}.
    Mutates data["source"], data["interaction2source"], data["interaction2link"].
    """
    seen: dict[str, int] = {}  # name → canonical id
    remap: dict[int, int] = {}
    unique_rows: list[tuple] = []

    for row in data["source"]:
        old_id, name = int(row[0]), row[1]
        if name in seen:
            remap[old_id] = seen[name]
        else:
            seen[name] = old_id
            unique_rows.append(row)

    if not remap:
        return remap

    data["source"] = unique_rows

    # interaction2source: (interaction_id, source_id)
    data["interaction2source"] = [
        (r[0], remap.get(int(r[1]), int(r[1]))) for r in data["interaction2source"]
    ]

    # interaction2link: (interaction_id, link, source_id, species_text)
    data["interaction2link"] = [
        (r[0], r[1], remap.get(int(r[2]), int(r[2])), r[3])
        for r in data["interaction2link"]
    ]

    return remap


def _deduplicate_proteins(data: dict[str, list[tuple]]) -> dict[int, int]:
    """
    Merge protein rows with the same name into one (keep lowest id).
    Returns {duplicate_old_id: canonical_id}.
    Mutates all tables that reference protein_id.
    """
    seen: dict[str, int] = {}  # name → canonical id
    remap: dict[int, int] = {}
    unique_rows: list[tuple] = []

    for row in data["protein"]:
        old_id, name = int(row[0]), row[1]
        if name in seen:
            remap[old_id] = seen[name]
        else:
            seen[name] = old_id
            unique_rows.append(row)

    if not remap:
        return remap

    data["protein"] = unique_rows

    def _rc0(rows: list[tuple]) -> list[tuple]:
        return [(remap.get(int(r[0]), int(r[0])), *r[1:]) for r in rows]

    def _rc12(rows: list[tuple]) -> list[tuple]:
        return [
            (
                r[0],
                remap.get(int(r[1]), int(r[1])),
                remap.get(int(r[2]), int(r[2])),
                *r[3:],
            )
            for r in rows
        ]

    data["protein2uniprot"] = _rc0(data["protein2uniprot"])
    data["protein2entrez"] = _rc0(data["protein2entrez"])
    data["protein2tissue"] = _rc0(data["protein2tissue"])

    for tbl in (
        "interaction",
        "homomint_interaction",
        "i2d_interaction",
        "ortho_interaction",
    ):
        data[tbl] = _rc12(data[tbl])

    return remap


def _deduplicate_entrez_mapping(
    data: dict[str, list[tuple]],
) -> dict[tuple[int, int], int]:
    """
    Drop duplicate (protein_id, gene_id) pairs in protein2entrez.

    The ProteinEntrez model has a unique constraint on (protein, gene_id)
    only — name is not part of the key. First occurrence wins (its name
    is kept). After protein dedup two rows can collapse onto the same
    canonical (protein, gene_id) pair; this function prevents the crash
    in bulk_create.

    NOTE: the earlier key ``gene_id + ":" + name`` dropped legitimate rows
    whenever two different proteins happened to share a gene_id (which is
    allowed by the new model). The key is now (protein_id, gene_id).
    """
    seen: set[tuple[int, int]] = set()
    remap: dict[tuple[int, int], int] = {}
    unique_rows: list[tuple] = []

    for row in data["protein2entrez"]:
        key = (int(row[0]), int(row[1]))
        if key in seen:
            remap[key] = 1
        else:
            seen.add(key)
            unique_rows.append(row)

    if not remap:
        return remap

    data["protein2entrez"] = unique_rows
    return remap


def _deduplicate_tissue_mapping(
    data: dict[str, list[tuple]],
) -> dict[tuple[int, int], int]:
    """
    Drop duplicate (protein_id, tissue_id) pairs in protein2tissue.

    The ProteinTissue model has a unique constraint on (protein, tissue).
    After protein dedup, two rows can collapse onto the same canonical pair
    and would crash bulk_create.

    NOTE: an earlier version keyed by ``str(pid) + "0" + str(tid)`` which is
    ambiguous (``pid=1, tid=102`` and ``pid=101, tid=2`` both collapse to
    "10102"). The key is now a proper tuple so there is no collision.
    """
    seen: set[tuple[int, int]] = set()
    remap: dict[tuple[int, int], int] = {}
    unique_rows: list[tuple] = []

    for row in data["protein2tissue"]:
        key = (int(row[0]), int(row[1]))
        if key in seen:
            remap[key] = 1
        else:
            seen.add(key)
            unique_rows.append(row)

    if not remap:
        return remap

    data["protein2tissue"] = unique_rows
    return remap


def _deduplicate_uniprot_accession_mapping(
    data: dict[str, list[tuple]],
) -> dict[str, str]:
    """
    Merge accession-uniprot mapping rows with the same accession and uniprot_id into one (keep lowest id).
    Returns {joint_key: joint_key}.
    """
    seen: dict[str, int] = {}  # name → canonical id
    remap: dict[str, str] = {}
    unique_rows: list[tuple] = []

    for row in data["uniprot_accession2id"]:
        accession, uniprot_id = row[0], row[1]
        joint_key = accession + ":" + uniprot_id
        if joint_key in seen:
            remap[joint_key] = joint_key
        else:
            seen[joint_key] = 1
            unique_rows.append(row)

    if not remap:
        return remap

    # Not referenced in any other classes

    data["uniprot_accession2id"] = unique_rows

    return remap


def _deduplicate_protein_uniprot_mapping(
    data: dict[str, list[tuple]],
) -> dict[tuple[int, str, int], int]:
    """
    Drop duplicate (protein_id, uniprot_id, version) triples in protein2uniprot.

    The ProteinUniProt model's unique constraint covers all three columns.
    After protein dedup two source rows can collapse onto the same canonical
    triple (e.g. two proteins with the same name pointing at the same UniProt
    entry) and would crash bulk_create.

    First row wins. Source rows where version is NULL are treated as version=0
    to match the import coercion in _import_proteins.
    """
    seen: set[tuple[int, str, int]] = set()
    remap: dict[tuple[int, str, int], int] = {}
    unique_rows: list[tuple] = []

    for row in data["protein2uniprot"]:
        version = int(row[3]) if row[3] is not None else 0
        key = (int(row[0]), str(row[1]), version)
        if key in seen:
            remap[key] = 1
        else:
            seen.add(key)
            unique_rows.append(row)

    if not remap:
        return remap

    data["protein2uniprot"] = unique_rows
    return remap


def _deduplicate_interaction_go_mapping(
    data: dict[str, list[tuple]],
) -> dict[str, str]:
    """
    Merge interaction-go mapping rows with the same interaction_id and term_id into one (keep lowest id).
    Returns {joint_key: joint_key}.
    """
    seen: dict[str, int] = {}  # name → canonical id
    remap: dict[str, str] = {}
    unique_rows: list[tuple] = []

    for row in data["interaction2GO"]:
        interaction_id, term_id = int(row[0]), row[1]
        joint_key = str(interaction_id) + ":" + term_id
        if joint_key in seen:
            remap[joint_key] = joint_key
        else:
            seen[joint_key] = 1
            unique_rows.append(row)

    if not remap:
        return remap

    # Not referenced in any other classes

    data["interaction2GO"] = unique_rows

    return remap


def _deduplicate_interaction_pmid_mapping(
    data: dict[str, list[tuple]],
) -> dict[str, str]:
    """
    Merge interaction-pmid mapping rows with the same interaction_id and pmid into one (keep lowest id).
    Returns {joint_key: joint_key}.
    """
    seen: dict[str, int] = {}  # name → canonical id
    remap: dict[str, str] = {}
    unique_rows: list[tuple] = []

    for row in data["interaction2pubmed"]:
        interaction_id, pmid = int(row[0]), int(row[1])
        joint_key = str(interaction_id) + ":" + str(pmid)
        if joint_key in seen:
            remap[joint_key] = joint_key
        else:
            seen[joint_key] = 1
            unique_rows.append(row)

    if not remap:
        return remap

    # Not referenced in any other classes

    data["interaction2pubmed"] = unique_rows

    return remap


def _deduplicate_interaction_source_mapping(
    data: dict[str, list[tuple]],
) -> dict[str, str]:
    """
    Merge interaction-source mapping rows with the same interaction_id and source_id into one (keep lowest id).
    Returns {joint_key: joint_key}.
    """
    seen: dict[str, int] = {}  # name → canonical id
    remap: dict[str, str] = {}
    unique_rows: list[tuple] = []

    for row in data["interaction2source"]:
        interaction_id, source_id = int(row[0]), int(row[1])
        joint_key = str(interaction_id) + ":" + str(source_id)
        if joint_key in seen:
            remap[joint_key] = joint_key
        else:
            seen[joint_key] = 1
            unique_rows.append(row)

    if not remap:
        return remap

    # Not referenced in any other classes

    data["interaction2source"] = unique_rows

    return remap


def _deduplicate_interaction_species_mapping(
    data: dict[str, list[tuple]],
) -> dict[str, str]:
    """
    Merge interaction-species mapping rows with the same interaction_id and species_id into one (keep lowest id).
    Returns {joint_key: joint_key}.
    """
    seen: dict[str, int] = {}  # name → canonical id
    remap: dict[str, str] = {}
    unique_rows: list[tuple] = []

    for row in data["interaction2species"]:
        interaction_id, species_id = int(row[0]), int(row[1])
        joint_key = str(interaction_id) + ":" + str(species_id)
        if joint_key in seen:
            remap[joint_key] = joint_key
        else:
            seen[joint_key] = 1
            unique_rows.append(row)

    if not remap:
        return remap

    # Not referenced in any other classes

    data["interaction2species"] = unique_rows

    return remap


def _deduplicate_interaction_type_mapping(
    data: dict[str, list[tuple]],
) -> dict[str, str]:
    """
    Merge interaction-type mapping rows with the same interaction_id and type_id into one (keep lowest id).
    Returns {joint_key: joint_key}.
    """
    seen: dict[str, int] = {}  # name → canonical id
    remap: dict[str, str] = {}
    unique_rows: list[tuple] = []

    for row in data["interaction2type"]:
        interaction_id, type_id = int(row[0]), int(row[1])
        joint_key = str(interaction_id) + ":" + str(type_id)
        if joint_key in seen:
            remap[joint_key] = joint_key
        else:
            seen[joint_key] = 1
            unique_rows.append(row)

    if not remap:
        return remap

    # Not referenced in any other classes

    data["interaction2type"] = unique_rows

    return remap


def _deduplicate_interaction_experiment_mapping(
    data: dict[str, list[tuple]],
) -> dict[str, str]:
    """
    Merge interaction-experiment mapping rows with the same interaction_id and experiment_id into one (keep lowest id).
    Returns {joint_key: joint_key}.
    """
    seen: dict[str, int] = {}  # name → canonical id
    remap: dict[str, str] = {}
    unique_rows: list[tuple] = []

    for row in data["interaction2experiment"]:
        interaction_id, experiment_id = int(row[0]), int(row[1])
        joint_key = str(interaction_id) + ":" + str(experiment_id)
        if joint_key in seen:
            remap[joint_key] = joint_key
        else:
            seen[joint_key] = 1
            unique_rows.append(row)

    if not remap:
        return remap

    # Not referenced in any other classes

    data["interaction2experiment"] = unique_rows

    return remap


def _deduplicate_interaction_effect_mapping(
    data: dict[str, list[tuple]],
) -> dict[str, str]:
    """
    Merge interaction-effect mapping rows with the same interaction_id, effect_type, and source into one (keep lowest id).
    Returns {joint_key: joint_key}.
    """
    seen: dict[str, int] = {}  # name → canonical id
    remap: dict[str, str] = {}
    unique_rows: list[tuple] = []

    for row in data["interaction2effect"]:
        interaction_id, effect_type, source = int(row[0]), int(row[1]), int(row[2])
        joint_key = str(interaction_id) + ":" + str(effect_type) + ":" + str(source)
        if joint_key in seen:
            remap[joint_key] = joint_key
        else:
            seen[joint_key] = 1
            unique_rows.append(row)

    if not remap:
        return remap

    # Not referenced in any other classes

    data["interaction2effect"] = unique_rows

    return remap


def _deduplicate_interactions(
    data: dict[str, list[tuple]],
    log: "ImportLog",
) -> dict[int, int]:
    """
    After protein dedup multiple source interactions may collapse onto the
    same canonical (protein_1_id, protein_2_id) pair. Keep lowest
    interaction_id (and its score), drop the rest, and remap all junction
    table references to the kept id so ``ignore_conflicts`` doesn't leave
    orphaned junction rows.

    Each dropped interaction is recorded in the log with its full row
    (id, p1, p2, score) and the id/score of the kept row so the caller
    can see what alternative score/id was discarded — this is the one
    piece of real data loss produced by dedup.
    Returns {duplicate_id: canonical_id}.
    """
    seen: dict[tuple[int, int], tuple[int, object]] = {}
    remap: dict[int, int] = {}

    for r in data["interaction"]:
        iid, p1, p2 = int(r[0]), int(r[1]), int(r[2])
        if p1 > p2:
            p1, p2 = p2, p1
        pair = (p1, p2)
        if pair in seen:
            kept_id, kept_score = seen[pair]
            remap[iid] = kept_id
            log.record(
                "interaction_merged",
                "interaction",
                r,
                f"canonical pair {pair} already taken by id={kept_id} "
                f"(kept score={kept_score}); this row dropped",
            )
        else:
            seen[pair] = (iid, r[3])

    if not remap:
        return remap

    duplicate_ids = set(remap.keys())
    data["interaction"] = [
        r for r in data["interaction"] if int(r[0]) not in duplicate_ids
    ]

    for tbl in (
        "interaction2GO",
        "interaction2effect",
        "interaction2experiment",
        "interaction2keggDirection",
        "interaction2link",
        "interaction2mesh",
        "interaction2pubmed",
        "interaction2source",
        "interaction2species",
        "interaction2type",
        "bait_prey_assoc",
    ):
        data[tbl] = [(remap.get(int(r[0]), int(r[0])), *r[1:]) for r in data[tbl]]

    return remap


def _filter_orphaned_protein_refs(
    data: dict[str, list[tuple]],
    log: "ImportLog",
) -> dict[str, int]:
    """
    Drop rows that reference a protein_id not present in data["protein"].
    Each dropped row is logged individually (category=orphan_protein) so the
    caller can inspect which protein_id was missing and which row was lost.
    Returns {table_name: dropped_count} for summary-print convenience.
    Mutates data in-place.
    """
    valid_pids: set[int] = {int(r[0]) for r in data["protein"]}
    dropped: dict[str, int] = {}

    # Tables where col 0 is protein_id
    for tbl in ("protein2uniprot", "protein2entrez", "protein2tissue"):
        kept: list[tuple] = []
        for r in data[tbl]:
            pid = int(r[0])
            if pid in valid_pids:
                kept.append(r)
            else:
                log.record(
                    "orphan_protein",
                    tbl,
                    r,
                    f"protein_id={pid} absent from protein table",
                )
        n = len(data[tbl]) - len(kept)
        data[tbl] = kept
        if n:
            dropped[tbl] = n

    # Tables where cols 1 and 2 are protein_1_id / protein_2_id
    for tbl in (
        "interaction",
        "homomint_interaction",
        "i2d_interaction",
        "ortho_interaction",
    ):
        kept = []
        for r in data[tbl]:
            p1, p2 = int(r[1]), int(r[2])
            if p1 in valid_pids and p2 in valid_pids:
                kept.append(r)
            else:
                missing = [p for p in (p1, p2) if p not in valid_pids]
                log.record(
                    "orphan_protein",
                    tbl,
                    r,
                    f"missing protein_id(s)={missing}",
                )
        n = len(data[tbl]) - len(kept)
        data[tbl] = kept
        if n:
            dropped[tbl] = n

    # Drop junction rows for interactions that were just removed
    valid_iids: set[int] = {int(r[0]) for r in data["interaction"]}
    for tbl in (
        "interaction2GO",
        "interaction2effect",
        "interaction2experiment",
        "interaction2keggDirection",
        "interaction2link",
        "interaction2mesh",
        "interaction2pubmed",
        "interaction2source",
        "interaction2species",
        "interaction2type",
        "bait_prey_assoc",
    ):
        kept = []
        for r in data[tbl]:
            iid = int(r[0])
            if iid in valid_iids:
                kept.append(r)
            else:
                log.record(
                    "orphan_protein",
                    tbl,
                    r,
                    f"parent interaction_id={iid} dropped earlier (orphan protein)",
                )
        n = len(data[tbl]) - len(kept)
        data[tbl] = kept
        if n:
            dropped[tbl] = n

    return dropped


def _filter_orphaned_lookup_refs(
    data: dict[str, list[tuple]],
    log: "ImportLog",
) -> dict[str, int]:
    """
    Drop junction rows that reference a lookup id (tissue / source / species /
    experiment / interaction-type / GO / MeSH) absent from the corresponding
    master table. Each dropped row is logged individually
    (category=orphan_lookup) so the missing id can be inspected.
    """
    dropped: dict[str, int] = {}

    def _filter(tbl: str, col: int, valid: set, lookup_name: str) -> None:
        kept: list[tuple] = []
        for r in data[tbl]:
            v = r[col]
            try:
                v_norm = int(v) if not isinstance(v, str) else v
            except (TypeError, ValueError):
                v_norm = v
            if v in valid or v_norm in valid:
                kept.append(r)
            else:
                log.record(
                    "orphan_lookup",
                    tbl,
                    r,
                    f"{lookup_name}={v!r} absent from {lookup_name} master table",
                )
        n = len(data[tbl]) - len(kept)
        data[tbl] = kept
        if n:
            dropped[tbl] = n

    valid_tissue = {r[0] for r in data["tissue"]}
    valid_source = {int(r[0]) for r in data["source"]}
    valid_species = {r[0] for r in data["species"]}
    valid_exp = {r[0] for r in data["experiment_type"]}
    valid_inttype = {r[0] for r in data["interaction_type"]}
    valid_go = {r[0] for r in data["GO_slim_term"]}
    valid_mesh = {r[0] for r in data["mesh_term"]}

    _filter("protein2tissue", 1, valid_tissue, "tissue_id")
    _filter("interaction2source", 1, valid_source, "source_id")
    _filter("interaction2link", 2, valid_source, "source_id")
    _filter("interaction2experiment", 1, valid_exp, "experiment_type_id")
    _filter("interaction2type", 1, valid_inttype, "interaction_type_id")
    _filter("interaction2GO", 1, valid_go, "go_term_id")
    _filter("interaction2mesh", 1, valid_mesh, "mesh_term_number")
    _filter("interaction2species", 1, valid_species, "species_id")

    return dropped


def parse_dump(path: Path) -> dict[str, list[tuple]]:
    """
    Single linear pass through the dump file.
    Returns {table_name: [row_tuples]} for every table in WANTED_TABLES.
    Encoding: latin-1 (original MariaDB charset).
    """
    data: dict[str, list[tuple]] = {t: [] for t in WANTED_TABLES}
    current: list[tuple] | None = None

    with open(path, encoding="latin-1", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\r\n")
            m = _INSERT_RE.match(line)
            if m:
                tname, rest = m.group(1), m.group(2).strip()
                current = data.get(tname)
                if current is not None and rest:
                    row = _parse_row(rest)
                    if row is not None:
                        current.append(row)
                continue
            if current is not None and line and line[0] == "(":
                row = _parse_row(line)
                if row is not None:
                    current.append(row)

    return data


# ---------------------------------------------------------------------------
# Import log
# ---------------------------------------------------------------------------


class ImportLog:
    """
    Per-row log of data losses and alterations.

    Writes a markdown pipe-table to `--log-file` so the user can open
    the file and inspect exactly which values were dropped or changed.
    Each row = one lost / altered value. Columns:
      category       — classification (see module docstring).
      source_table   — old-schema table the value came from.
      lost_row       — JSON of the full source row (or a descriptive dict
                       when the loss is columnar rather than row-level).
      reason         — free-text explanation of what was done / why.
    """

    COLS: tuple[str, ...] = ("category", "source_table", "lost_row", "reason")

    def __init__(self, path: Path) -> None:
        self._path = path
        self._entries: list[tuple[str, str, str, str]] = []

    def record(
        self,
        category: str,
        table: str,
        row: object,
        reason: str,
    ) -> None:
        self._entries.append((category, table, self._serialize(row), reason))

    @staticmethod
    def _serialize(row: object) -> str:
        if isinstance(row, tuple):
            row = list(row)
        return json.dumps(row, default=str, ensure_ascii=False)

    def flush(self) -> int:
        def esc(s: str) -> str:
            return str(s).replace("|", "\\|").replace("\n", " ").replace("\r", " ")

        lines = [
            "| " + " | ".join(self.COLS) + " |",
            "|" + "|".join("---" for _ in self.COLS) + "|",
        ]
        for entry in self._entries:
            lines.append("| " + " | ".join(esc(c) for c in entry) + " |")

        with open(self._path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        return len(self._entries)

    def category_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for cat, _, _, _ in self._entries:
            counts[cat] = counts.get(cat, 0) + 1
        return counts

    def __len__(self) -> int:
        return len(self._entries)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _chunked(lst: list, n: int) -> Iterator[list]:
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


def _bulk(
    model: Any,
    objs: list,
    batch_size: int,
    *,
    ignore_conflicts: bool = False,
) -> None:
    if objs:
        model.objects.bulk_create(
            objs,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
        )


def _reset_sequence(model: Any) -> None:
    """
    After bulk-inserting rows with explicit PKs, MySQL/MariaDB's
    AUTO_INCREMENT counter must be bumped past the max PK so that
    subsequent inserts don't collide.
    No-op on SQLite (it auto-tracks max+1).
    """
    if connection.vendor != "mysql":
        return
    table = model._meta.db_table
    pk_col = model._meta.pk.column
    with connection.cursor() as cur:
        cur.execute(f"SELECT MAX(`{pk_col}`) FROM `{table}`")
        (max_id,) = cur.fetchone()
        if max_id is not None:
            cur.execute(f"ALTER TABLE `{table}` AUTO_INCREMENT = {max_id + 1}")


# ---------------------------------------------------------------------------
# Management command
# ---------------------------------------------------------------------------


class Command(BaseCommand):
    help = "Import a HIPPIE v2 MariaDB SQL dump into Django models."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("sql_file", help="Path to the MariaDB SQL dump (.sql).")
        parser.add_argument(
            "--batch-size",
            type=int,
            default=5_000,
            metavar="N",
            help="Bulk-insert batch size (default: 5000).",
        )
        parser.add_argument(
            "--log-file",
            default="import_hippie.log",
            metavar="PATH",
            help="Write skipped/replaced rows to this JSON file (default: import_hippie.log).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Parse the dump and report row counts — no DB writes.",
        )

    # ------------------------------------------------------------------

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: C901
        # Late import so the app registry is fully loaded.
        from hippie_website.models import (
            BaitPreyAssociation,
            BaitPreyTest,
            ExperimentType,
            GOSlimTerm,
            Interaction,
            InteractionCrossReference,
            InteractionPublication,
            InteractionType,
            MeSHTerm,
            OrthologInteraction,
            Protein,
            ProteinEntrez,
            ProteinTissue,
            ProteinUniProt,
            SignalingEndpoint,
            Source,
            Species,
            Tissue,
            UniProtAccession,
        )

        path = Path(options["sql_file"])
        if not path.exists():
            raise CommandError(f"File not found: {path}")

        log = ImportLog(Path(options["log_file"]))
        bs: int = options["batch_size"]
        dry: bool = options["dry_run"]

        self.stdout.write(f"Parsing {path} …")
        data = parse_dump(path)

        # Dedup passes — silent. They merge by natural key, so no info loss
        # except for _deduplicate_interactions, which logs per dropped row.
        _deduplicate_sources(data)
        _deduplicate_proteins(data)
        _deduplicate_entrez_mapping(data)
        _deduplicate_tissue_mapping(data)
        _deduplicate_protein_uniprot_mapping(data)
        _deduplicate_uniprot_accession_mapping(data)
        _deduplicate_interactions(data, log)
        _deduplicate_interaction_go_mapping(data)
        _deduplicate_interaction_pmid_mapping(data)
        _deduplicate_interaction_source_mapping(data)
        _deduplicate_interaction_species_mapping(data)
        _deduplicate_interaction_type_mapping(data)
        _deduplicate_interaction_experiment_mapping(data)
        _deduplicate_interaction_effect_mapping(data)

        # Orphan filters — log per dropped row.
        _filter_orphaned_protein_refs(data, log)
        _filter_orphaned_lookup_refs(data, log)

        if dry:
            self.stdout.write(self.style.WARNING("Dry-run — no DB writes."))
            for tname in sorted(WANTED_TABLES):
                n = len(data[tname])
                if n:
                    self.stdout.write(f"  {tname}: {n:,} rows")
            return

        with transaction.atomic():
            self._import_lookup_tables(
                data, bs, Tissue, Species, Source, ExperimentType, InteractionType
            )
            self._import_go(data, bs, GOSlimTerm)
            self._import_mesh(data, bs, MeSHTerm)
            self._import_proteins(
                data,
                log,
                bs,
                Protein,
                ProteinUniProt,
                ProteinEntrez,
                ProteinTissue,
                UniProtAccession,
            )
            self._import_signaling(data, bs, SignalingEndpoint)
            swapped = self._import_interactions(data, log, bs, Interaction)
            self._update_effect(data, log, bs, swapped, Interaction)
            self._update_kegg(data, log, bs, swapped, Interaction)
            self._import_interaction_m2m(data, bs, Interaction, InteractionPublication)
            self._import_cross_refs(data, log, bs, InteractionCrossReference, Species)
            self._import_ortholog(data, log, bs, OrthologInteraction, Species)
            self._import_bait_prey(
                data,
                log,
                bs,
                swapped,
                BaitPreyAssociation,
                BaitPreyTest,
                ExperimentType,
            )

        n_issues = log.flush()
        if n_issues:
            counts = log.category_counts()
            self.stdout.write(
                self.style.WARNING(
                    f"{n_issues:,} lost / altered values logged → {options['log_file']}"
                )
            )
            for cat in sorted(counts):
                self.stdout.write(f"  {cat:<22} {counts[cat]:>8,}")
        self.stdout.write(self.style.SUCCESS("Import complete."))

    # ------------------------------------------------------------------
    # Phase helpers
    # ------------------------------------------------------------------

    def _say(self, msg: str) -> None:
        self.stdout.write(msg)

    def _import_lookup_tables(
        self,
        data: dict,
        bs: int,
        Tissue: Any,
        Species: Any,
        Source: Any,
        ExperimentType: Any,
        InteractionType: Any,
    ) -> None:
        self._say("Importing lookup tables …")

        _bulk(Tissue, [Tissue(id=r[0], name=r[1]) for r in data["tissue"]], bs)
        _reset_sequence(Tissue)
        self._say(f"  tissue:           {len(data['tissue']):>8,}")

        _bulk(Species, [Species(id=r[0], name=r[1]) for r in data["species"]], bs)
        _reset_sequence(Species)
        self._say(f"  species:          {len(data['species']):>8,}")

        _bulk(
            Source,
            [Source(id=r[0], name=r[1], url=r[2] or "") for r in data["source"]],
            bs,
        )
        _reset_sequence(Source)
        self._say(f"  source:           {len(data['source']):>8,}")

        _bulk(
            ExperimentType,
            [
                ExperimentType(
                    id=r[0],
                    name=r[1],
                    psi_mi_code=r[2] or "",
                    quality_score=float(r[3]),
                )
                for r in data["experiment_type"]
            ],
            bs,
        )
        _reset_sequence(ExperimentType)
        self._say(f"  experiment_type:  {len(data['experiment_type']):>8,}")

        _bulk(
            InteractionType,
            [
                InteractionType(id=r[0], name=r[1], psi_mi_code=r[2] or "")
                for r in data["interaction_type"]
            ],
            bs,
        )
        _reset_sequence(InteractionType)
        self._say(f"  interaction_type: {len(data['interaction_type']):>8,}")

    def _import_go(self, data: dict, bs: int, GOSlimTerm: Any) -> None:
        self._say("Importing GO slim terms …")

        _bulk(
            GOSlimTerm,
            [
                GOSlimTerm(id=r[0], name=r[1], namespace=r[2])
                for r in data["GO_slim_term"]
            ],
            bs,
        )
        self._say(f"  GO_slim_term:     {len(data['GO_slim_term']):>8,}")

        # Self-referential M2M (symmetrical=False):
        #   from_goslimterm_id = child term (the row that "has parents")
        #   to_goslimterm_id   = parent term
        # Old table: (term_id=child, parent_term_id=parent)
        Through = GOSlimTerm.parents.through
        _bulk(
            Through,
            [
                Through(from_goslimterm_id=r[0], to_goslimterm_id=r[1])
                for r in data["GO_slim_term2term"]
            ],
            bs,
            ignore_conflicts=False,
        )
        self._say(f"  GO_slim_term2term:{len(data['GO_slim_term2term']):>8,}")

    def _import_mesh(self, data: dict, bs: int, MeSHTerm: Any) -> None:
        self._say("Importing MeSH terms …")
        _bulk(
            MeSHTerm, [MeSHTerm(number=r[0], name=r[1]) for r in data["mesh_term"]], bs
        )
        self._say(f"  mesh_term:        {len(data['mesh_term']):>8,}")

    def _import_proteins(
        self,
        data: dict,
        log: ImportLog,
        bs: int,
        Protein: Any,
        ProteinUniProt: Any,
        ProteinEntrez: Any,
        ProteinTissue: Any,
        UniProtAccession: Any,
    ) -> None:
        self._say("Importing proteins …")

        _bulk(Protein, [Protein(id=r[0], name=r[1]) for r in data["protein"]], bs)
        _reset_sequence(Protein)
        self._say(f"  protein:          {len(data['protein']):>8,}")

        # Old cols: (protein_id, uniprot_id, uniprot_db_id, version)
        # uniprot_db_id is discarded — not present in new model. Log every
        # row whose uniprot_db_id was non-NULL so the actually-lost values
        # are inspectable, plus one summary row.
        # discarded_db_ids = 0
        # for r in data["protein2uniprot"]:
        #    if r[2] is not None:
        #        log.record(
        #            "column_discarded",
        #            "protein2uniprot",
        #            r,
        #            f"uniprot_db_id={r[2]!r} dropped — no column in ProteinUniProt",
        #        )
        #        discarded_db_ids += 1
        # if discarded_db_ids:
        #    log.record(
        #        "column_discarded",
        #        "protein2uniprot",
        #        {"column": "uniprot_db_id", "rows_with_value": discarded_db_ids},
        #        "summary: total source rows whose uniprot_db_id was lost",
        #    )
        _bulk(
            ProteinUniProt,
            [
                ProteinUniProt(
                    protein_id=r[0],
                    uniprot_id=r[1],
                    version=r[3] if r[3] is not None else 0,
                )
                for r in data["protein2uniprot"]
            ],
            bs,
            ignore_conflicts=False,
        )
        self.stdout.write(
            self.style.WARNING(
                "Check whether 'version' should be part of unique key proteinUniProt"
            )
        )
        self._say(f"  protein2uniprot:  {len(data['protein2uniprot']):>8,}")

        _bulk(
            ProteinEntrez,
            [
                ProteinEntrez(protein_id=r[0], gene_id=r[1], name=r[2] or "")
                for r in data["protein2entrez"]
            ],
            bs,
            ignore_conflicts=False,
        )
        self.stdout.write(self.style.WARNING("Protein Entrez is not used anywhere"))
        self._say(f"  protein2entrez:   {len(data['protein2entrez']):>8,}")

        pt_rows = []
        # skipped = 0
        for r in data["protein2tissue"]:
            # if r[1] == 0:
            #    skipped += 1
            #    continue
            pt_rows.append(ProteinTissue(protein_id=r[0], tissue_id=r[1]))
        _bulk(ProteinTissue, pt_rows, bs, ignore_conflicts=False)
        self._say(
            f"  protein2tissue:   {len(pt_rows):>8,}"  #  (skipped {skipped} with tissue_id=0)"
        )

        _bulk(
            UniProtAccession,
            [
                UniProtAccession(accession=r[0], uniprot_id=r[1])
                for r in data["uniprot_accession2id"]
                if r[1]
            ],
            bs,
            ignore_conflicts=False,
        )
        self._say(f"  uniprot_acc2id:   {len(data['uniprot_accession2id']):>8,}")

    def _import_signaling(self, data: dict, bs: int, SignalingEndpoint: Any) -> None:
        self._say("Importing signaling endpoints …")
        _bulk(
            SignalingEndpoint,
            [
                SignalingEndpoint(uniprot_id=r[0], type=r[1])
                for r in data["sp_analysis_end_nodes"]
            ],
            bs,
            ignore_conflicts=False,
        )
        self._say(f"  sp_analysis_end_nodes: {len(data['sp_analysis_end_nodes']):>5,}")

    def _import_interactions(
        self,
        data: dict,
        log: ImportLog,
        bs: int,
        Interaction: Any,
    ) -> set[int]:
        """
        Import interaction table with canonical-order enforcement.
        Returns the set of interaction IDs whose protein pair was swapped
        (needed to invert kegg_direction and bait-prey direction later).
        """
        self._say("Importing interactions …")
        objs: list = []
        swapped: set[int] = set()
        null_score = 0

        for r in data["interaction"]:
            iid, p1, p2, score = int(r[0]), int(r[1]), int(r[2]), r[3]
            if p1 > p2:
                p1, p2 = p2, p1
                swapped.add(iid)
            if score is None:
                log.record(
                    "null_score",
                    "interaction",
                    r,
                    "score was NULL in source — imported as 0.0",
                )
                null_score += 1
                score = 0.0
            objs.append(
                Interaction(
                    id=iid,
                    protein_1_id=p1,
                    protein_2_id=p2,
                    score=float(score),
                )
            )

        _bulk(Interaction, objs, bs, ignore_conflicts=False)
        _reset_sequence(Interaction)
        self._say(
            f"  interaction:      {len(objs):>8,}"
            f"  (swapped={len(swapped):,}, null_score={null_score:,})"
        )
        return swapped

    def _update_effect(
        self,
        data: dict,
        log: ImportLog,
        bs: int,
        swapped: set[int],
        Interaction: Any,
    ) -> None:
        """
        Inline interaction2effect (effect_type, effect_source) onto Interaction rows.
        Multiple rows per interaction: last row wins; superseded rows are logged.
        """
        effect_map: dict[int, tuple[int, int]] = {}
        for r in data["interaction2effect"]:
            iid, etype, esrc = int(r[0]), int(r[1]), int(r[2])
            if iid in effect_map:
                prev_etype, prev_esrc = effect_map[iid]
                log.record(
                    "effect_superseded",
                    "interaction2effect",
                    {
                        "interaction_id": iid,
                        "superseded_effect_type": prev_etype,
                        "superseded_effect_source": prev_esrc,
                        "kept_effect_type": etype,
                        "kept_effect_source": esrc,
                    },
                    "multiple rows for same interaction_id — last-row-wins; "
                    "earlier (effect_type, effect_source) discarded",
                )
            effect_map[iid] = (etype, esrc)

        if not effect_map:
            return

        total = 0
        for batch_items in _chunked(list(effect_map.items()), bs):
            ids = [iid for iid, _ in batch_items]
            objs = list(
                Interaction.objects.filter(id__in=ids).only(
                    "id", "effect_type", "effect_source"
                )
            )
            for obj in objs:
                obj.effect_type, obj.effect_source = effect_map[obj.id]
            Interaction.objects.bulk_update(
                objs, ["effect_type", "effect_source"], batch_size=bs
            )
            total += len(objs)

        self._say(f"  interaction2effect: {total:>6,} updated")

    def _update_kegg(
        self,
        data: dict,
        log: ImportLog,
        bs: int,
        swapped: set[int],
        Interaction: Any,
    ) -> None:
        """
        Inline interaction2keggDirection onto Interaction.kegg_direction.
        Direction is inverted for any interaction whose pair was swapped during import.
        """
        kegg_map: dict[int, int] = {}
        for r in data["interaction2keggDirection"]:
            iid, direction = int(r[0]), int(r[1])
            if iid in swapped:
                direction = -direction
            kegg_map[iid] = direction

        if not kegg_map:
            return

        total = 0
        for batch_items in _chunked(list(kegg_map.items()), bs):
            ids = [iid for iid, _ in batch_items]
            objs = list(
                Interaction.objects.filter(id__in=ids).only("id", "kegg_direction")
            )
            for obj in objs:
                obj.kegg_direction = kegg_map[obj.id]
            Interaction.objects.bulk_update(objs, ["kegg_direction"], batch_size=bs)
            total += len(objs)

        self._say(f"  interaction2keggDir:{total:>5,} updated")

    def _import_interaction_m2m(
        self,
        data: dict,
        bs: int,
        Interaction: Any,
        InteractionPublication: Any,
    ) -> None:
        self._say("Importing interaction M2M tables …")

        # Through-table column naming follows Django convention:
        #   left side  → interaction_id
        #   right side → {lowercase_model_name}_id

        Through = Interaction.go_terms.through
        _bulk(
            Through,
            [
                Through(interaction_id=r[0], goslimterm_id=r[1])
                for r in data["interaction2GO"]
            ],
            bs,
            ignore_conflicts=False,
        )
        self._say(f"  interaction2GO:     {len(data['interaction2GO']):>6,}")

        Through = Interaction.mesh_terms.through
        _bulk(
            Through,
            [
                Through(interaction_id=r[0], meshterm_id=r[1])
                for r in data["interaction2mesh"]
            ],
            bs,
            ignore_conflicts=False,
        )
        self._say(f"  interaction2mesh:   {len(data['interaction2mesh']):>6,}")

        _bulk(
            InteractionPublication,
            [
                InteractionPublication(interaction_id=r[0], pmid=r[1])
                for r in data["interaction2pubmed"]
            ],
            bs,
            ignore_conflicts=False,
        )
        self._say(f"  interaction2pubmed: {len(data['interaction2pubmed']):>6,}")

        Through = Interaction.sources.through
        _bulk(
            Through,
            [
                Through(interaction_id=r[0], source_id=r[1])
                for r in data["interaction2source"]
            ],
            bs,
            ignore_conflicts=False,
        )
        self._say(f"  interaction2source: {len(data['interaction2source']):>6,}")

        Through = Interaction.conserved_species.through
        _bulk(
            Through,
            [
                Through(interaction_id=r[0], species_id=r[1])
                for r in data["interaction2species"]
            ],
            bs,
            ignore_conflicts=False,
        )
        self._say(f"  interaction2species:{len(data['interaction2species']):>6,}")

        Through = Interaction.interaction_types.through
        _bulk(
            Through,
            [
                Through(interaction_id=r[0], interactiontype_id=r[1])
                for r in data["interaction2type"]
            ],
            bs,
            ignore_conflicts=False,
        )
        self._say(f"  interaction2type:   {len(data['interaction2type']):>6,}")

        Through = Interaction.experiments.through
        _bulk(
            Through,
            [
                Through(interaction_id=r[0], experimenttype_id=r[1])
                for r in data["interaction2experiment"]
            ],
            bs,
            ignore_conflicts=False,
        )
        self._say(f"  interaction2expt:   {len(data['interaction2experiment']):>6,}")

    def _import_cross_refs(
        self,
        data: dict,
        log: ImportLog,
        bs: int,
        InteractionCrossReference: Any,
        Species: Any,
    ) -> None:
        self._say("Importing cross-references …")

        # Build prefix-match cache: short species name → Species.id
        # Species table stores full names like "Mus musculus (House mouse) ...";
        # interaction2link stores short names like "Mus musculus".
        all_species = list(Species.objects.values_list("id", "name"))
        species_cache: dict[str, int | None] = {}

        def _lookup(text: str) -> int | None:
            if text not in species_cache:
                hit = next(
                    (sid for sid, sname in all_species if sname.startswith(text)), None
                )
                if hit is None:
                    log.record(
                        "species_unmatched",
                        "interaction2link",
                        {"species_text": text},
                        "no Species row with name starting with this text → "
                        "cross_reference.species_id set NULL",
                    )
                species_cache[text] = hit
            return species_cache[text]

        objs = []
        for r in data["interaction2link"]:
            interaction_id, link, source_id, species_text = r[0], r[1], r[2], r[3]
            species_id = _lookup(species_text) if species_text else None
            objs.append(
                InteractionCrossReference(
                    interaction_id=interaction_id,
                    link=link or "",
                    source_id=source_id,
                    species_id=species_id,
                )
            )

        _bulk(InteractionCrossReference, objs, bs, ignore_conflicts=False)
        self._say(f"  interaction2link:   {len(objs):>6,}")

    def _import_ortholog(
        self,
        data: dict,
        log: ImportLog,
        bs: int,
        OrthologInteraction: Any,
        Species: Any,
    ) -> None:
        """
        Import homomint, i2d, ortho interactions into OrthologInteraction.

        Old tables have separate auto-increment IDs that overlap across sources,
        so we cannot preserve PKs. Strategy:
          1. Deduplicate pairs per source after canonical-order normalisation.
          2. bulk_create, then re-query to get assigned PKs.
          3. Bulk-insert species M2M using the re-queried PK mapping.
        """
        self._say("Importing ortholog interactions …")

        _SOURCES = [
            ("homomint", "homomint_interaction", "homomint_interaction2species"),
            ("i2d", "i2d_interaction", "i2d_interaction2species"),
            ("ortho", "ortho_interaction", "ortho_interaction2species"),
        ]

        for source_val, int_table, sp_table in _SOURCES:
            # Build old_id → normalised (p1, p2)
            old_to_pair: dict[int, tuple[int, int]] = {}
            for r in data[int_table]:
                old_id, p1, p2 = int(r[0]), int(r[1]), int(r[2])
                if p1 > p2:
                    p1, p2 = p2, p1
                old_to_pair[old_id] = (p1, p2)

            # Build old_id → {species_ids}
            old_to_species: dict[int, set[int]] = defaultdict(set)
            for r in data[sp_table]:
                old_to_species[int(r[0])].add(int(r[1]))

            # Merge species across any pairs that normalised to the same canonical form
            pair_to_species: dict[tuple[int, int], set[int]] = defaultdict(set)
            for old_id, pair in old_to_pair.items():
                pair_to_species[pair].update(old_to_species.get(old_id, set()))

            # Insert unique pairs
            unique_pairs = list(pair_to_species.keys())
            _bulk(
                OrthologInteraction,
                [
                    OrthologInteraction(
                        protein_1_id=p1, protein_2_id=p2, source=source_val
                    )
                    for p1, p2 in unique_pairs
                ],
                bs,
                ignore_conflicts=False,
            )
            self._say(f"  {int_table}: {len(unique_pairs):,}")

            # Re-query to get assigned PKs
            pair_to_oi_id: dict[tuple[int, int], int] = {
                (p1, p2): oi_id
                for oi_id, p1, p2 in OrthologInteraction.objects.filter(
                    source=source_val
                ).values_list("id", "protein_1_id", "protein_2_id")
            }

            # Insert species M2M
            Through = OrthologInteraction.ortholog_species.through
            species_objs = []
            for pair, species_ids in pair_to_species.items():
                oi_id = pair_to_oi_id.get(pair)
                if oi_id is None:
                    log.record(
                        "ortho_unresolved",
                        int_table,
                        {"pair": list(pair), "species_ids": sorted(species_ids)},
                        "OrthologInteraction row not found after bulk_create — "
                        "attached species rows skipped",
                    )
                    continue
                for sid in species_ids:
                    species_objs.append(
                        Through(orthologinteraction_id=oi_id, species_id=sid)
                    )

            _bulk(Through, species_objs, bs, ignore_conflicts=False)
            self._say(f"  {sp_table}: {len(species_objs):,}")

    def _import_bait_prey(
        self,
        data: dict,
        log: ImportLog,
        bs: int,
        swapped: set[int],
        BaitPreyAssociation: Any,
        BaitPreyTest: Any,
        ExperimentType: Any,
    ) -> None:
        """
        Import bait_prey_assoc → BaitPreyAssociation + BaitPreyTest.

        Old schema: (interaction_id, pmid, direction)
        New schema requires a BaitPreyTest FK (method + detection + pmid).
        A placeholder ExperimentType is created for the unknown method.
        detection defaults to True (all old records are positive associations).
        """
        self._say("Importing bait-prey associations …")

        placeholder, _ = ExperimentType.objects.get_or_create(
            name="Unknown (legacy import)",
            defaults={"psi_mi_code": "", "quality_score": 0.0},
        )
        if data["bait_prey_assoc"]:
            log.record(
                "placeholder_exp_type",
                "bait_prey_assoc",
                {
                    "rows": len(data["bait_prey_assoc"]),
                    "placeholder_experiment_type_id": placeholder.id,
                    "placeholder_name": placeholder.name,
                },
                "source has no method/detection info — every BaitPreyTest gets "
                "the placeholder ExperimentType and detection=True",
            )

        # Group by (interaction_id, direction) → set of pmids
        groups: dict[tuple[int, int], set[int]] = defaultdict(set)
        for r in data["bait_prey_assoc"]:
            iid, pmid, direction = int(r[0]), int(r[1]), int(r[2])
            if iid in swapped:
                direction = -direction
            groups[(iid, direction)].add(pmid)

        # Pre-create all BaitPreyTest objects (unique per pmid given fixed method+detection)
        all_pmids = sorted({pmid for pmids in groups.values() for pmid in pmids})
        pmid_to_test: dict[int, Any] = {}
        for pmid in all_pmids:
            test, _ = BaitPreyTest.objects.get_or_create(
                detection=True,
                pmid=pmid,
                method=placeholder,
            )
            pmid_to_test[pmid] = test

        # Create associations
        assoc_count = 0
        for (interaction_id, direction), pmids in groups.items():
            try:
                assoc, _ = BaitPreyAssociation.objects.get_or_create(
                    interaction_id=interaction_id,
                    direction=direction,
                )
            except Exception as exc:
                log.record(
                    "bait_prey_error",
                    "bait_prey_assoc",
                    {
                        "interaction_id": interaction_id,
                        "direction": direction,
                        "pmids": sorted(pmids),
                    },
                    f"BaitPreyAssociation.get_or_create raised: {exc!r}",
                )
                continue
            tests = [pmid_to_test[p] for p in pmids if p in pmid_to_test]
            if tests:
                assoc.tests_performed.add(*tests)
            assoc_count += 1

        self._say(
            f"  bait_prey_assoc:    {assoc_count:>6,} associations, "
            f"{len(pmid_to_test):,} tests"
        )
