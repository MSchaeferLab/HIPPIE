from .hippie_update import get_human_gene_map
from ._sources import data_path, mod_sources, stream_url
from ._mitab import open_mitab
from collections.abc import Iterator
from django.core.management.base import BaseCommand
from hippie_website.models import Gene, Interaction, Species, OrthologInteraction
import gzip
import io
import re
import sys
import urllib.request


TAXONS = {
    "10116": ("RGD", "Rattus norvegicus"),
    "10090": ("MGI", "Mus musculus"),
    "7955": ("ZFIN", "Danio rerio"),
    "8364": ("Xenbase", "Xenopus tropicalis"),
    "8355": ("Xenbase", "Xenopus laevis"),
    "7227": ("FlyBase", "Drosophila melanogaster"),
    "6239": ("WormBase", "Caenorhabditis elegans"),
    "559292": ("SGD", "Saccharomyces cerevisiae"),
}

INTACT_FTP_URL = stream_url("intact_full")

MIN_AGREEMENT_SCORE_RATIO = 0.8

UNIPROT_RE = re.compile(
    r"\b(?:[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})\b"
)
GENE_RE = {
    "FlyBase": re.compile(r"FBgn\d+"),
    "WormBase": re.compile(r"WBGene\d+"),
    "SGD": re.compile(r"\bS\d{9}\b"),
    "ZFIN": re.compile(r"ZDB-GENE-\d+-\d+"),
    "MGI": re.compile(r"MGI:\d+"),
}
# Per-species MOD gene→UniProt sources, reconstructed from data/sources.json.
# Each entry: {db, gz, and either 'url' (live fetch) or 'file' (local path)}.
SOURCES = mod_sources()

_TAXON_LOOKUP: set[str] = set(TAXONS) | {"9606"}
_TAXID_LEN = len("taxid:")


# ---------------------------------------------------------------------------
# MOD gene→UniProt mapping helpers (still needed to resolve species UniProt IDs)
# ---------------------------------------------------------------------------


def _open(url: str):
    return urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": "protein-mapper/1.0"}),
        timeout=120,
    )


def _lines(taxon: str, conf_dict: dict):
    if taxon == "6239":
        try:
            raw = open(conf_dict["file"], "rb")
            wrap = gzip.GzipFile(fileobj=raw)
        except FileNotFoundError:
            sys.stdout.write(
                "Please download the wormbase file before and add it to the data folder: https://downloads.wormbase.org/releases/current-production-release/species/c_elegans/PRJNA13758/annotation/c_elegans.PRJNA13758.WS298.xrefs.txt.gz"
            )
            exit(1)
    else:
        raw = _open(conf_dict["url"])
        wrap = gzip.GzipFile(fileobj=raw) if conf_dict.get("gz") else raw
    for line in io.TextIOWrapper(wrap, encoding="utf-8", errors="replace"):
        yield line.rstrip("\n")


def parse_xenbase(lines, taxon_id: str) -> list[tuple[str, str]]:
    taxon_tag = f"taxon:{taxon_id}"
    gene2uniprot = []
    for line in lines:
        if line.startswith("!"):
            continue
        parts = line.split("\t")
        if len(parts) <= 8 or taxon_tag not in parts[6]:
            continue
        gene_id = parts[1]
        for ref in parts[8].split("|"):
            if "UniProtKB:" in ref:
                gene2uniprot.append((ref.replace("UniProtKB:", ""), gene_id))
    return gene2uniprot


def parse_db(gene_re, lines) -> list[tuple[str, str]]:
    gene2uniprot = []
    for line in lines:
        genes = gene_re.findall(line)
        if genes:
            uniprot_ids = UNIPROT_RE.findall(line)
            for gene_id in genes:
                for uniprot_id in uniprot_ids:
                    gene2uniprot.append((uniprot_id, gene_id))
    return gene2uniprot


def parse_rgd(lines) -> list[tuple[str, str]]:
    gene2uniprot = []
    for line in lines:
        if line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) <= 21:
            continue
        gene_id = parts[0]
        for uniprot_id in parts[21].split(";"):
            uniprot_id = uniprot_id.strip()
            if uniprot_id:
                gene2uniprot.append((uniprot_id, gene_id))
    return gene2uniprot


def _parse_lines(conf_dict: dict, taxon_id: str, lines) -> list[tuple[str, str]]:
    if conf_dict["db"] == "Xenbase":
        return parse_xenbase(lines, taxon_id)
    if conf_dict["db"] == "RGD":
        return parse_rgd(lines)
    return parse_db(GENE_RE[conf_dict["db"]], lines)


def write_all_mapping(output_file: str) -> None:
    """Write taxon\\tdb\\tuniprot\\tgene_id for every configured MOD source."""
    written = 0
    with open(output_file, "w") as w:
        for taxon, conf_dict in SOURCES.items():
            print(f"[{conf_dict['db']} {taxon}] ...", file=sys.stderr)
            pairs = _parse_lines(conf_dict, taxon, _lines(taxon, conf_dict))
            for uniprot_id, gene_id in pairs:
                w.write(f"{taxon}\t{conf_dict['db']}\t{uniprot_id}\t{gene_id}\n")
            written += len(pairs)
            print(f"  {len(pairs):,} pairs", file=sys.stderr)
    print(f"Done — {written:,} mappings written to {output_file}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Orthology helpers
# ---------------------------------------------------------------------------


def read_orthology_data(
    file_path: str, hgnc_to_entrez_dict: dict[str, str]
) -> dict[str, list[list[str]]]:
    """Parse the genome alliance orthology file; return HGNC ID → [[other_gene_id, taxon], ...]."""
    orthology_data: dict[str, list[list[str]]] = {}
    with open(file_path, "r") as file:
        for line in file:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("Gene1ID"):
                continue
            parts = line.split("\t")
            is_human1 = ":9606" in parts[2]
            is_human2 = ":9606" in parts[6]

            if not is_human1 ^ is_human2:
                continue

            score_ratio = int(parts[9]) / int(parts[10])
            if score_ratio < MIN_AGREEMENT_SCORE_RATIO:
                continue

            if is_human1:
                hs_gene_id = parts[0].replace("HGNC:", "")
                other_gene_id = parts[4]
                other_species = parts[6].split(":")[1]
            else:
                hs_gene_id = parts[4].replace("HGNC:", "")
                other_gene_id = parts[0]
                other_species = parts[2].split(":")[1]

            if "MGI" not in other_gene_id:
                other_gene_id = other_gene_id.split(":")[1]

            if hs_gene_id not in hgnc_to_entrez_dict:
                continue

            orthology_data.setdefault(hs_gene_id, []).append(
                [other_gene_id, other_species]
            )

    return orthology_data


def hgnc_id_to_entrez_id(
    ncbi_gene_info_file_path: str,
) -> tuple[dict[str, str], dict[str, str]]:
    """Parse NCBI gene_info for human genes; return (hgnc→entrez, entrez→hgnc)."""
    hgnc_to_entrez_dict: dict[str, str] = {}
    entrez_id_to_hgnc_dict: dict[str, str] = {}
    for line in open(ncbi_gene_info_file_path, "r"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if parts[0] != "9606":
            continue
        entrez_id = parts[1]
        for ref in parts[5].split("|"):
            if ref.startswith("HGNC:HGNC:"):
                hgnc_id = ref.split(":")[2]
                if hgnc_id in hgnc_to_entrez_dict:
                    raise ValueError(f"Duplicate HGNC ID found: {hgnc_id}")
                hgnc_to_entrez_dict[hgnc_id] = entrez_id
                entrez_id_to_hgnc_dict[entrez_id] = hgnc_id
                break
    return hgnc_to_entrez_dict, entrez_id_to_hgnc_dict


def get_updated_uniprot_dict(uniprot_idmap_file_path: str) -> dict[str, str]:
    """Read the UniProt secondary-to-primary AC file; return old_id → current_id."""
    updated_uniprot_dict: dict[str, str] = {}
    header_found = False
    with open(uniprot_idmap_file_path, "r") as file:
        for line in file:
            line = line.strip()
            if not header_found or not line:
                if line.startswith("____________"):
                    header_found = True
                continue
            parts = re.split(r"\s+", line)
            updated_uniprot_dict[parts[0]] = parts[1]
    return updated_uniprot_dict


def _build_species_to_human(
    orthology_data: dict[str, list[list[str]]],
    hgnc_to_entrez: dict[str, str],
) -> dict[tuple[str, str], set[int]]:
    """Build (taxon_id_str, species_gene_id) → set[human_entrez_id] reverse lookup."""
    result: dict[tuple[str, str], set[int]] = {}
    for hgnc_id, orthologs in orthology_data.items():
        entrez = hgnc_to_entrez.get(hgnc_id)
        if entrez is None:
            continue
        for species_gene_id, taxon in orthologs:
            result.setdefault((taxon, species_gene_id), set()).add(int(entrez))
    return result


# ---------------------------------------------------------------------------
# IntAct streaming
# ---------------------------------------------------------------------------


def _taxon_of(field: str) -> str | None:
    """Return the relevant taxon ID string from a MITAB taxon field, or None."""
    idx = field.find("taxid:")
    if idx == -1:
        return None
    start = idx + _TAXID_LEN
    end = start
    while end < len(field) and field[end].isdigit():
        end += 1
    tid = field[start:end]
    return tid if tid in _TAXON_LOOKUP else None


def _stream_intact(source: str | None = None) -> Iterator[tuple[str, str, str, str]]:
    """
    Stream the full IntAct MITAB; yield (raw_a, raw_b, taxon_a, taxon_b).
    source: local file path (plain or .gz) or None to fetch from INTACT_FTP_URL.
    """
    import os

    src = source if (source and os.path.exists(source)) else INTACT_FTP_URL
    for parts in open_mitab(src):
        # open_mitab already skips '#'/blank lines; drop the MITAB header row too.
        if parts[0].startswith("ID"):
            continue
        if len(parts) < 11:
            continue
        taxon_a = _taxon_of(parts[9])
        taxon_b = _taxon_of(parts[10])
        if taxon_a is None or taxon_b is None:
            continue
        if not (taxon_a in TAXONS or taxon_b in TAXONS):
            continue
        if taxon_a != taxon_b and taxon_a != "9606" and taxon_b != "9606":
            continue
        raw_a = parts[0].replace("uniprotkb:", "").split("-")[0]
        raw_b = parts[1].replace("uniprotkb:", "").split("-")[0]
        if not raw_a or not raw_b or "|" in raw_a or "|" in raw_b:
            continue
        yield raw_a, raw_b, taxon_a, taxon_b


# ---------------------------------------------------------------------------
# OrthologInteraction registration helper
# ---------------------------------------------------------------------------


def _register_edge(
    d: dict,
    key: tuple[int, int],
    species: "Species",
) -> None:
    if key not in d:
        d[key] = (OrthologInteraction(gene_1_id=key[0], gene_2_id=key[1]), {species})
    else:
        d[key][1].add(species)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def update_homology_data(
    homology_file: str,
    ncbi_gene_info_file: str,
    intact_file: str | None = None,
    stdout=sys.stdout,
) -> None:
    """Populate OrthologInteraction and species links by streaming the full IntAct MITAB."""

    def log(msg: str) -> None:
        stdout.write(msg + "\n")

    # Build MOD UniProt→gene TSV cache
    uni_prot_mod_map_file = str(data_path("uniprot_to_orthology"))
    log("Fetching MOD gene→UniProt mappings...")
    write_all_mapping(uni_prot_mod_map_file)

    # Load all species uniprot→gene in one pass
    log("Loading all species UniProt→gene mappings...")
    all_species_u2g: dict[str, dict[str, set[str]]] = {t: {} for t in TAXONS}
    with open(uni_prot_mod_map_file, "r") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 4:
                continue
            taxon, uniprot_id, gene_id = parts[0], parts[2], parts[3]
            if taxon not in all_species_u2g:
                continue
            all_species_u2g[taxon].setdefault(uniprot_id, set()).add(gene_id)
    for t in TAXONS:
        log(f"  {TAXONS[t][1]} ({t}): {len(all_species_u2g[t]):,} UniProt entries")

    log("Loading NCBI gene info...")
    hgnc_to_entrez_dict, _ = hgnc_id_to_entrez_id(ncbi_gene_info_file)
    log(f"  {len(hgnc_to_entrez_dict):,} HGNC genes mapped")

    log("Loading orthology data...")
    orthology_data = read_orthology_data(homology_file, hgnc_to_entrez_dict)
    log(f"  {len(orthology_data):,} human genes with orthologs")

    log("Building species→human reverse orthology...")
    species_to_human = _build_species_to_human(orthology_data, hgnc_to_entrez_dict)
    log(f"  {len(species_to_human):,} (taxon, species_gene) keys")

    log("Loading UniProt secondary AC map...")
    updated_uniprot_dict = get_updated_uniprot_dict(str(data_path("sec_ac")))
    log(f"  {len(updated_uniprot_dict):,} ID updates")

    log("Loading human UniProt→entrez map...")
    gene_map, _, _ = get_human_gene_map()
    log(f"  {len(gene_map):,} human UniProt entries")

    log("Building gene PK map...")
    gene_pk_map: dict[int, int] = dict(Gene.objects.values_list("entrez_id", "pk"))
    log(f"  {len(gene_pk_map):,} genes indexed")

    log("Loading human interaction pairs from DB...")
    human_pairs_set: set[frozenset] = set()
    entrez_pair_to_gene_pks: dict[frozenset, tuple[int, int]] = {}
    for e1, e2 in (
        Interaction.objects.all()
        .values_list("protein_1__gene__entrez_id", "protein_2__gene__entrez_id")
        .distinct()
    ):
        if e1 is None or e2 is None:
            continue
        pk1 = gene_pk_map.get(e1)
        pk2 = gene_pk_map.get(e2)
        if pk1 is None or pk2 is None:
            continue
        fs = frozenset({e1, e2})
        human_pairs_set.add(fs)
        entrez_pair_to_gene_pks[fs] = tuple(sorted((pk1, pk2)))
    log(f"  {len(human_pairs_set):,} distinct human gene pairs")

    # Ensure Species objects exist for all target taxons
    species_objs: dict[str, Species] = {}
    for taxon_id, (_, taxon_name) in TAXONS.items():
        species_objs[taxon_id], _ = Species.objects.get_or_create(
            name=taxon_name, NCBI_tax_id=int(taxon_id)
        )

    # Stream IntAct and register ortholog interactions
    orthointeractions_to_create: dict[
        tuple[int, int], tuple[OrthologInteraction, set[Species]]
    ] = {}
    n_lines = 0
    n_registered = 0

    source_label = intact_file if intact_file else INTACT_FTP_URL
    log(f"Streaming IntAct from {source_label} ...")
    mapped_interactions: dict[tuple[str, bool], int] = dict()
    for taxon in TAXONS.keys():
        for mixed_case in [True, False]:
            mapped_interactions[(taxon, mixed_case)] = 0

    for raw_a, raw_b, taxon_a, taxon_b in _stream_intact(intact_file):
        n_lines += 1
        if n_lines % 100_000 == 0:
            stdout.write(f"\r  Lines: {n_lines:,}  registered: {n_registered:,}")
            stdout.flush()

        # Canonicalize both accessions via secondary-AC map
        uniprot_a = updated_uniprot_dict.get(raw_a, raw_a)
        uniprot_b = updated_uniprot_dict.get(raw_b, raw_b)

        if taxon_a == "9606" or taxon_b == "9606":
            # species × human interaction
            if taxon_a == "9606":
                human_up, species_up, taxon = uniprot_a, uniprot_b, taxon_b
            else:
                human_up, species_up, taxon = uniprot_b, uniprot_a, taxon_a
            human_info = gene_map.get(human_up)
            if not human_info or human_info[0] is None:
                continue
            human_entrez = int(human_info[0])
            for sg in all_species_u2g[taxon].get(species_up, ()):
                for h2 in species_to_human.get((taxon, sg), ()):
                    if h2 == human_entrez:
                        continue
                    fs = frozenset({human_entrez, h2})
                    if fs not in human_pairs_set:
                        continue
                    gene_pair_id = entrez_pair_to_gene_pks[fs]
                    _register_edge(
                        orthointeractions_to_create, gene_pair_id, species_objs[taxon]
                    )
                    n_registered += 1
                    mapped_interactions[(taxon, True)] += 1
        else:
            # species × species (same taxon)
            taxon = taxon_a
            for ga in all_species_u2g[taxon].get(uniprot_a, ()):
                for ha in species_to_human.get((taxon, ga), ()):
                    for gb in all_species_u2g[taxon].get(uniprot_b, ()):
                        for hb in species_to_human.get((taxon, gb), ()):
                            if ha == hb:
                                continue
                            fs = frozenset({ha, hb})
                            if fs not in human_pairs_set:
                                continue
                            gene_pair_id = entrez_pair_to_gene_pks[fs]
                            _register_edge(
                                orthointeractions_to_create,
                                gene_pair_id,
                                species_objs[taxon],
                            )
                            n_registered += 1
                            mapped_interactions[(taxon, False)] += 1

    log(
        f"\nStreaming done — {n_lines:,} lines, {len(orthointeractions_to_create):,} unique gene pairs"
    )
    log(f"{'Species':<35} {'species×human':>15} {'species×species':>16}")
    for taxon_id, (_, taxon_name) in TAXONS.items():
        sh = mapped_interactions[(taxon_id, True)]
        ss = mapped_interactions[(taxon_id, False)]
        log(f"  {taxon_name:<33} {sh:>15,} {ss:>16,}")

    # Bulk-create OrthologInteractions
    oi_objs = [oi for oi, _ in orthointeractions_to_create.values()]
    log(f"Bulk creating {len(oi_objs):,} OrthologInteractions...")
    OrthologInteraction.objects.bulk_create(oi_objs, ignore_conflicts=True)

    pair_pk_map: dict[tuple, int] = {
        (oi.gene_1_id, oi.gene_2_id): oi.pk
        for oi in OrthologInteraction.objects.filter(
            gene_1_id__in=[gp[0] for gp in orthointeractions_to_create],
            gene_2_id__in=[gp[1] for gp in orthointeractions_to_create],
        )
    }

    Through = OrthologInteraction.ortholog_species.through
    through_rows = []
    for gene_pair_id, (_, species_set) in orthointeractions_to_create.items():
        oi_pk = pair_pk_map.get(gene_pair_id)
        if oi_pk is None:
            continue
        for species in species_set:
            through_rows.append(
                Through(orthologinteraction_id=oi_pk, species_id=species.pk)
            )
    log(f"Writing {len(through_rows):,} species links...")
    Through.objects.bulk_create(through_rows, ignore_conflicts=True)
    log("Done.")


class Command(BaseCommand):
    help = "Populate OrthologInteraction by streaming full IntAct MITAB via FTP"

    def add_arguments(self, parser):
        parser.add_argument(
            "--homology_file",
            type=str,
            default=str(data_path("orthology_alliance")),
            help="Path to genome alliance orthology TSV (default: from data/sources.json)",
        )
        parser.add_argument(
            "--ncbi_gene_info_file",
            type=str,
            default=str(data_path("gene_info")),
            help="Path to NCBI human gene_info file (default: from data/sources.json)",
        )
        parser.add_argument(
            "--intact_file",
            type=str,
            default=None,
            help="Local IntAct MITAB file (.txt or .gz); if omitted, streams from FTP",
        )

    def handle(self, *args, **options):
        update_homology_data(
            options["homology_file"],
            options["ncbi_gene_info_file"],
            intact_file=options["intact_file"],
            stdout=self.stdout,
        )
