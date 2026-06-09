from .hippie_update import get_human_gene_map
from django.core.management.base import BaseCommand
from django.db.models import F
from hippie_website.models import Gene, Interaction, Species, OrthologInteraction
import gzip, io, os, re, sys, urllib.parse, urllib.request
import networkx as nx


TAXONS = {
    "10116": ("RGD", "rat.txt", "Rattus norvegicus"),
    "10090": ("MGI", "mouse.txt", "Mus musculus"),
    "7955": ("ZFIN", "danre.txt", "Danio rerio"),
    "8364": ("Xenbase", "xentr.txt", "Xenopus tropicalis"),
    "8355": ("Xenbase", "xenla.txt", "Xenopus laevis"),
    "7227": ("FlyBase", "drome.txt", "Drosophila melanogaster"),
    "6239": ("WormBase", "caeel.txt", "Caenorhabditis elegans"),
    "559292": ("SGD", "yeast.txt", "Saccharomyces cerevisiae"),
}

PREFIX = {"RGD", "MGI", "ZFIN", "Xenbase", "Xenbase", "FlyBase", "WormBase", "SGD"}
MIN_AGREEMENT_SCORE_RATIO = 0.8 # 5/6 > 0.8



IDMAP_BASE = "https://ftp.uniprot.org/pub/databases/uniprot/current_release/knowledgebase/idmapping/by_organism/"

UNIPROT_RE = re.compile(r"\b(?:[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})\b")
GENE_RE = {
    "FlyBase": re.compile(r"FBgn\d+"), "WormBase": re.compile(r"WBGene\d+"),
    "SGD": re.compile(r"\bS\d{9}\b"), "ZFIN": re.compile(r"ZDB-GENE-\d+-\d+"),
    "MGI": re.compile(r"MGI:\d+"),
}
SOURCES = {
    "10116": dict(db="RGD", url="https://download.rgd.mcw.edu/data_release/GENES_RAT.txt", gz=0, rgd=1),
    "10090": dict(db="MGI", url="https://www.informatics.jax.org/downloads/reports/MRK_SwissProt_TrEMBL.rpt", gz=0),
    "7955":  dict(db="ZFIN", url="https://zfin.org/downloads/uniprot.txt", gz=0),
    "8364":  dict(db="Xenbase", url="https://download.xenbase.org/xenbase/GenePageReports/xenbase_v1.2.gpi.gz", gz=1),
    "8355":  dict(db="Xenbase", url="https://download.xenbase.org/xenbase/GenePageReports/xenbase_v1.2.gpi.gz", gz=1),
    "7227":  dict(db="FlyBase", gz=1, comment="#",
                  url=f"https://s3ftp.flybase.org/releases/current/precomputed_files/genes/fbgn_NAseq_Uniprot_fb_2026_01.tsv.gz"),
    "6239":  dict(db="WormBase", file="data/species/c_elegans.PRJNA13758.WS298.xrefs.txt.gz", gz=1),
    "559292": dict(db="SGD", url="http://sgd-archive.yeastgenome.org/curation/chromosomal_feature/dbxref.tab", gz=0),
}


def _open(url):
    return urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": "protein-mapper/1.0"}), timeout=120)


def _lines(taxon, conf_dict):
    if taxon =="6239":
        raw = open(conf_dict["file"], "rb")
        wrap = gzip.GzipFile(fileobj=raw)
    else:
        raw = _open(conf_dict["url"])
        wrap = gzip.GzipFile(fileobj=raw) if conf_dict.get("gz") else raw
    for line in io.TextIOWrapper(wrap, encoding="utf-8", errors="replace"):
        yield line.rstrip("\n")


def parse_xenbase(lines, taxon_id):
    taxon_tag = f"taxon:{taxon_id}"
    gene2uniprot = []
    for line in lines:
        if line.startswith("!"):
            continue
        
        parts = line.split("\t")        
        if taxon_tag not in parts[6]:
            continue
        
        gene_id = parts[1]
        
        uniprot_ids = []
        xref = parts[8].split("|")
        for ref in xref:
            if "UniProtKB:" in ref:
                uniprot_ids.append(ref.replace("UniProtKB:", ""))
        
        for uniprot_id in uniprot_ids:
            gene2uniprot.append((uniprot_id, gene_id))
    return gene2uniprot


def parse_db(gene_re, lines):
    gene2uniprot = []
    for line in lines:
        genes = gene_re.findall(line)
        if genes:
            uniprot_ids = UNIPROT_RE.findall(line)
            for gene_id in genes:
                for uniprot_id in uniprot_ids:
                    gene2uniprot.append((uniprot_id, gene_id))
    return gene2uniprot

def parse_rgd(lines):
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

def _parse_lines(conf_dict, taxon_id, lines):
    if conf_dict["db"] == "Xenbase":
        return parse_xenbase(lines, taxon_id)
    
    if conf_dict["db"] == "RGD":
        return parse_rgd(lines)

    return parse_db(GENE_RE[conf_dict["db"]], lines)
    


def write_all_mapping(output_file):
    """Write taxon \\t uniprot \\t db \\t gene_id for every configured MOD source."""
    written = 0
    with open(output_file, "w") as w:
        for taxon, conf_dict in SOURCES.items():
            print(f"[{conf_dict['db']} {taxon}] ...", file=sys.stderr)
            lines = _parse_lines(conf_dict, taxon, _lines(taxon, conf_dict))
            for line in lines:
                w.write(f"{taxon}\t{conf_dict['db']}\t{'\t'.join(line)}\n")
            written += len(lines)
            print(f"  {len(lines):,} pairs", file=sys.stderr)
    print(f"Done — {written:,} mappings written to {output_file}", file=sys.stderr)


##
# Read orthology data and create a mapping of human gene to other species genes
##


def read_orthology_data(file_path, hgnc_to_entrez_dict):
    """Parse the genome alliance orthology file and return a dict mapping HGNC ID → list of [other_gene_id, other_species] pairs."""
    orthology_data: dict[str, list[list[str]]] = {}
    with open(file_path, "r") as file:
        for line in file:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("Gene1ID"):
                continue
            parts = line.split("\t")
            is_human1 = ":9606" in parts[2]
            is_human2 = ":9606" in parts[6]

            if not is_human1 ^ is_human2:  # dont care about human human
                continue

            score_ratio = int(parts[9]) / int(
                parts[10]
            )  # AlgorithmsMatch  OutOfAlgorithm
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
                other_gene_id = other_gene_id.split(":")[1]  # MGI remain in id, but the other not

            if hs_gene_id not in hgnc_to_entrez_dict:
                continue  # if there is no mapping, skip

            if hs_gene_id not in orthology_data:
                orthology_data[hs_gene_id] = []
            orthology_data[hs_gene_id].append([other_gene_id, other_species])

    return orthology_data


def hgnc_id_to_entrez_id(ncbi_gene_info_file_path):
    """Parse NCBI gene_info for human genes and return (hgnc→entrez, entrez→hgnc) dicts (both keyed by string)."""
    hgnc_to_entrez_dict = dict()
    entrez_id_to_hgnc_dict = dict()
    for line in open(ncbi_gene_info_file_path, "r"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        parts = line.split("\t")
        tax_id = parts[0]
        if tax_id != "9606":
            continue
        entrez_id = parts[1]
        hgnc_id = None
        exref = parts[5]
        for ref in exref.split("|"):
            if ref.startswith("HGNC:HGNC:"):
                hgnc_id = ref.split(":")[2]
                if hgnc_id in hgnc_to_entrez_dict:
                    raise ValueError(f"Duplicate HGNC ID found: {hgnc_id}")
                hgnc_to_entrez_dict[hgnc_id] = entrez_id
                entrez_id_to_hgnc_dict[entrez_id] = hgnc_id
                break

    return hgnc_to_entrez_dict, entrez_id_to_hgnc_dict


def get_updated_uniprot_dict(uniprot_onthology_idmap_file_path):
    """Read the UniProt secondary-to-primary AC mapping file and return a dict of old_id → current_id."""
    updated_uniprot_dict = dict()
    header_found = False
    with open(uniprot_onthology_idmap_file_path, "r") as file:
        for line in file:
            line = line.strip()
            if not header_found or not line:
                if line.startswith("____________"):
                    header_found = True
                continue

            parts = re.split(r"\s+", line)
            updated_uniprot_dict[parts[0]] = parts[1]
    return updated_uniprot_dict


def get_species_edges(
    taxon_to_ppi_file_path, taxon_id, gene_id_mapping, updated_uniprot_dict, human_uniprot_mapping
):
    """Build PPI graph for a species; return (G, total_proteins_seen, proteins_with_gene_mapping)."""
    taxon_tag = f"taxid:{taxon_id}"
    human_tag = f"taxid:9606"
    all_proteins: set[str] = set()
    mapped_proteins: set[str] = set()
    skipped_due_to_taxon = 0
    skipped_due_to_missing_mapped = 0
    kept_human_taxon = 0
    kept_taxon_taxon = 0
    non_mapped = set()
    G = nx.Graph()
    with open(taxon_to_ppi_file_path, "r") as file:
        for line in file:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")

            is_human = [human_tag in p for p in parts[9:11]]
            is_taxon = [taxon_tag in p for p in parts[9:11]]

            keep = False
            if sum(is_human) == 1 and sum(is_taxon) == 1:
                keep = True
                kept_human_taxon += 1
            elif sum(is_taxon) == 2:
                keep = True
                kept_taxon_taxon += 1

            if not keep:
                skipped_due_to_taxon += 1
                continue

            ids = [p.replace("uniprotkb:", "") for p in parts[0:2]]
            ids = [id.split("-")[0] for id in ids]
            ids = [updated_uniprot_dict.get(id, id) for id in ids]
            all_proteins |= set(ids)

            mappings = [False, False]
            for i, (id, mask) in enumerate(zip(ids, is_human)):
                if mask:
                    mappings[i] = {id}  # already canonicalized via updated_uniprot_dict

            for i, (id, mask) in enumerate(zip(ids, is_taxon)):
                if mask:
                    mappings[i] = gene_id_mapping.get(id, {})


            if not all(map(bool, mappings)):
                if not mappings[0]:
                    non_mapped.add(ids[0])
                else:
                    non_mapped.add(ids[1])
                skipped_due_to_missing_mapped += 1
                continue
            
            for g1 in mappings[0]:
                mapped_proteins.add(g1)
                for g2 in mappings[1]:
                    mapped_proteins.add(g2)
                    G.add_edge(g1, g2)
    return (
        G,
        len(all_proteins),
        len(mapped_proteins),
        skipped_due_to_taxon,
        skipped_due_to_missing_mapped,
        kept_human_taxon,
        kept_taxon_taxon,
    )


def taxon_specific_gene_map(all_uniprot_gene_file, taxon_id, db_id):
    """Return a uniprot_id → gene_id mapping for a given taxon and database (e.g. RGD, MGI) from the cached TSV."""
    mapping = dict()
    with open(all_uniprot_gene_file, "r") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if parts[0] != taxon_id:
                continue
            
            uniprot_id = parts[2]
            gene_id = parts[3]
            if uniprot_id not in mapping:
                mapping[uniprot_id] = set()
            mapping[uniprot_id].add(gene_id)

    return mapping

def get_entrez_to_uniprot() -> dict[int, set[str]]:
    uniprot_to_entrez, _, _ = get_human_gene_map()
    entrez_to_uniprot: dict[int, set[str]] = {}
    for unip_acc, (entrez, _) in uniprot_to_entrez.items():
        if entrez is None:
            continue
        entrez_to_uniprot.setdefault(int(entrez), set()).add(unip_acc)
    return entrez_to_uniprot


def _register_edge(
    d: dict,
    key: tuple,
    species: "Species",
) -> None:
) -> None:
    if key not in d:
        d[key] = (OrthologInteraction(gene_1_id=key[0], gene_2_id=key[1]), {species})
    else:
        d[key][1].add(species)


def update_homology_data(homology_file, ncbi_gene_info_file, stdout=sys.stdout):
    """Populate OrthologInteraction and their species links for all configured taxons."""

    def log(msg: str, **kwargs) -> None:
        print(msg, file=stdout, **kwargs)

    pairs = list(
        Interaction.objects.all()
        .values_list(
            "protein_1__gene__entrez_id",
            "protein_2__gene__entrez_id",
        )
        .distinct()
    )
    seen: set[frozenset] = set()
    dedup_pairs = []
    for p in pairs:
        key = frozenset(p)
        if key not in seen:
            seen.add(key)
            dedup_pairs.append(p)
    pairs = dedup_pairs
    
    uni_prot_mod_map_file = "data/species/uniprot_to_orthology.tsv"
    write_all_mapping(uni_prot_mod_map_file) 

    log("Loading NCBI gene info...")
    hgnc_to_entrez_dict, entrez_to_hgnc_dict = hgnc_id_to_entrez_id(ncbi_gene_info_file)
    log(f"  {len(hgnc_to_entrez_dict):,} HGNC genes mapped")

    log("Loading orthology data...")
    orthology_data = read_orthology_data(homology_file, hgnc_to_entrez_dict)
    log(f"  {len(orthology_data):,} human genes with orthologs")

    log("Loading UniProt update map...")
    updated_uniprot_dict = get_updated_uniprot_dict("data/sec_ac.txt")
    log(f"  {len(updated_uniprot_dict):,} ID updates")

    log("Building gene PK map...")
    gene_pk_map: dict[int, int] = dict(Gene.objects.values_list("entrez_id", "pk"))
    log(f"  {len(gene_pk_map):,} genes indexed")

    log("Loading human entrez to human uniprot ...")
    entrez2uniprot_human = get_entrez_to_uniprot()

    n_taxons = len(TAXONS)
    for i, taxon in enumerate(TAXONS):
        taxon_id = taxon
        identifier_prefix = TAXONS[taxon][0]
        taxon_name = TAXONS[taxon][2]
        log(f"\n[{i + 1}/{n_taxons}] {taxon_name} ({identifier_prefix})")

        taxon_specific_gene_mapping = taxon_specific_gene_map(
            uni_prot_mod_map_file, taxon_id, identifier_prefix
        )
        log(f"  UniProt mapping: {len(taxon_specific_gene_mapping):,} entries")

        taxon_to_ppi_file_path = f"data/species/{TAXONS[taxon][1]}"
        (
            G,
            n_proteins_total,
            n_proteins_mapped,
            skipped_taxon,
            skipped_mapping,
            kept_human_taxon,
            kept_taxon_taxon,
        ) = get_species_edges(
            taxon_to_ppi_file_path,
            taxon_id,
            taxon_specific_gene_mapping,
            updated_uniprot_dict,
            entrez2uniprot_human,
        )
        log(f"  PPI network kept: {G.number_of_nodes():,} genes, {G.number_of_edges():,} edges")
        log(f"  Edges kept: {kept_human_taxon:,} human×taxon, {kept_taxon_taxon:,} taxon×taxon")
        log(f"  Edges skipped: {skipped_taxon:,} due to taxon mismatch")
        log(f"  Edges skipped: {skipped_mapping:,} due to missing mapping")
        log(f"  Proteins in file: {n_proteins_total:,} total, {n_proteins_mapped:,} mapped to a gene")

        current_species, _ = Species.objects.get_or_create(
            name=taxon_name, NCBI_tax_id=int(taxon_id)
        )

        orthointeractions_to_create: dict[
            tuple[int, int], tuple[OrthologInteraction, set[Species]]
        ] = {}
        n_pairs = 0
        tt_count = 0
        th_count = 0
        for pair in pairs:
            n_pairs += 1
            if n_pairs % 5_000 == 0:
                print(
                    f"\r  Pairs scanned: {n_pairs:,}  interactions found: {len(orthointeractions_to_create):,}",
                    end="",
                    flush=True,
                )
            ortho_gene_1 = orthology_data.get(
                entrez_to_hgnc_dict.get(str(pair[0])), False
            )
            ortho_gene_2 = orthology_data.get(
                entrez_to_hgnc_dict.get(str(pair[1])), False
            )
            if not any([ortho_gene_1, ortho_gene_2]):
                continue

            ortho_gene_1 = [gene for gene in (ortho_gene_1 or []) if gene[1] == taxon_id]
            ortho_gene_2 = [gene for gene in (ortho_gene_2 or []) if gene[1] == taxon_id]

            human_uniprots_1 = entrez2uniprot_human.get(pair[0], set())
            human_uniprots_2 = entrez2uniprot_human.get(pair[1], set())

            pk1 = gene_pk_map.get(pair[0])
            pk2 = gene_pk_map.get(pair[1])
            if pk1 is None or pk2 is None:
                continue
            gene_pair_id = tuple(sorted((pk1, pk2)))

            added = False
            for gene_1 in ortho_gene_1:
                for gene_2 in ortho_gene_2:
                    if (
                        G.has_edge(gene_1[0], gene_2[0])
                        or any(G.has_edge(hp, gene_2[0]) for hp in human_uniprots_1)
                        or any(G.has_edge(hp, gene_1[0]) for hp in human_uniprots_2)
                    ):
                        _register_edge(orthointeractions_to_create, gene_pair_id, current_species)
                        added = True
                        if G.has_edge(gene_1[0], gene_2[0]):
                            tt_count += 1
                        if any(G.has_edge(hp, gene_2[0]) for hp in human_uniprots_1):
                            th_count += 1
                        if any(G.has_edge(hp, gene_1[0]) for hp in human_uniprots_2):
                            th_count += 1
                        
                        break
                if added:
                    break

        log(f"\n  Homologous interactions taxon x taxon: {tt_count}, human x taxon {th_count}")
        oi_objs = [oi for oi, _ in orthointeractions_to_create.values()]  # oioi oi
        log(f"  Bulk creating {len(oi_objs):,} OrthologInteractions...")
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

            for species in species_set:
                through_rows.append(
                    Through(orthologinteraction_id=oi_pk, species_id=species.pk)
                )
        log(f"  Writing {len(through_rows):,} species links...")
        Through.objects.bulk_create(through_rows, ignore_conflicts=True)
        log(f"  Done.")


class Command(BaseCommand):
    help = "Update ortholog interaction data from PANTHER and species PPI files"

    def add_arguments(self, parser):
        parser.add_argument(
            "--homology_file", type=str, help="Path to genome alliance orthology file"
        )
        parser.add_argument(
            "--ncbi_gene_info_file", type=str, help="Path to NCBI human gene_info file"
        )

    def handle(self, *args, **options):
        update_homology_data(
            options["homology_file"], options["ncbi_gene_info_file"], stdout=sys.stdout
        )
