
from django.core.management.base import BaseCommand
from hippie_website.models import Gene, Interaction, Species, OrthologInteraction
import gzip, io, os, re, sys, urllib.request
import networkx as nx
import numpy as np


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

PREFIX = {
    "RGD",
    "MGI",
    "ZFIN",
    "Xenbase",
    "Xenbase",
    "FlyBase",
    "WormBase",
    "SGD"}
MIN_AGREEMENT_SCORE_RATIO = 0.8


##
# Get Uniprot-MOD id data
##

def stream_lines(source):
    """Stream lines from a gzipped URL, printing a progress counter to stderr every 500k lines."""
    raw = urllib.request.urlopen(source)
    wrap = gzip.GzipFile(fileobj=raw)
    count = 0
    try:
        for line in io.TextIOWrapper(wrap, encoding="utf-8"):
            count += 1
            if  count % 500_000 == 0:
                print(f"\r  Lines scanned: {count:>12,}", end="", file=sys.stderr, flush=True)
            yield line.rstrip("\n")
    finally:
        print(f"\r  Lines scanned: {count:>12,}", file=sys.stderr)
        raw.close()


def get_mod2uniprot(url):
    """Yield (tax_id, uniprot_id, db_name, db_id) rows from the UniProt idmapping.dat.gz for relevant species and DB prefixes."""
    prev_id = None
    captured = np.full(4, "", dtype=object)
    for line in stream_lines(url):
        if line.startswith("#") or not line.strip():
            continue

        parts = line.split("\t")
        id = parts[0]
        if id != prev_id:
            captured = np.full(4, "", dtype=object)

        prev_id = id
        if parts[1] == "NCBI_TaxID":
            tax_id = parts[2]
            if tax_id not in TAXONS:
                continue
            else:
                captured[0] = tax_id
        if parts[1] not in PREFIX:
            continue
        captured[1:] = parts
        yield captured


def write_all_mapping(url, output_file):
    """Download the UniProt ID mapping and write filtered rows to output_file as TSV."""
    written = 0
    with open(output_file, "w") as w:
        for line in get_mod2uniprot(url):
            row = "\t".join(line) + "\n"
            w.write(row)
            written += 1
    print(f"Done — {written:,} mappings written to {output_file}", file=sys.stderr)


##
# Read orthology data and create a mapping of human gene to other species genes
##


def read_othology_data(file_path, hgnc_to_entrez_dict):
    """Parse the genome alliance orthology file and return a dict mapping HGNC ID → list of [other_gene_id, other_species] pairs."""
    onthology_data: dict[str, list[str]] = {}
    with open(file_path, 'r') as file:
        for line in file:
            line = line.strip()
            if not line or line.startswith('#') or line.startswith("Gene1ID"):
                continue
            parts = line.split('\t')
            is_human1 = ":9606" in parts[2]
            is_human2 = ":9606" in parts[6]
            
            if not is_human1^is_human2: # dont care about human human
                continue
            
            score_ratio = int(parts[9])/int(parts[10]) # AlgorithmsMatch  OutOfAlgorithm 
            if score_ratio < MIN_AGREEMENT_SCORE_RATIO:
                continue
            
            if is_human1:
                hs_gene_id = parts[0].replace("HGNC:","")
                other_gene_id = parts[4]
                other_species = parts[6].split(":")[1]
            else:
                hs_gene_id = parts[4].replace("HGNC:","")
                other_gene_id = parts[0]
                other_species = parts[2].split(":")[1]
            
            if "MGI" not in other_gene_id:
                other_gene_id = other_gene_id.split(":")[1] # MGI remain in uniprot.dat, but the other not
                
            
            
            if hs_gene_id not in hgnc_to_entrez_dict:
                continue # if there is no mapping, skip
            
            if hs_gene_id not in onthology_data:
                onthology_data[hs_gene_id] = []
            onthology_data[hs_gene_id].append([other_gene_id, other_species]) 
            
    return onthology_data
            

def hgnc_id_to_entrez_id(ncbi_gene_info_file_path):
    """Parse NCBI gene_info for human genes and return (hgnc→entrez, entrez→hgnc) dicts (both keyed by string)."""
    hgnc_to_entrez_dict = dict()
    entrez_id_to_hgnc_dict = dict()
    for line in open(ncbi_gene_info_file_path, 'r'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        
        parts = line.split('\t')
        tax_id = parts[0]
        if tax_id != "9606":
            continue
        entrez_id = parts[1]
        hgnc_id = None
        exref = parts[5]
        for ref in exref.split('|'):
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
    with open(uniprot_onthology_idmap_file_path, 'r') as file:
        for line in file:
            line = line.strip()
            if not header_found or not line:
                if line.startswith("____________"):
                    header_found = True
                continue
            
            parts = re.split(r"\s+", line)
            updated_uniprot_dict[parts[0]] = parts[1]
    return updated_uniprot_dict


def get_species_edges(taxon_to_ppi_file_path, taxon_id, gene_id_mapping, updated_uniprot_dict):
    """Yield (gene_id_1, gene_id_2) pairs from a species MITAB PPI file, filtered to interactions within the target taxon."""
    taxon_tag = f"taxid:{taxon_id}"
    with open(taxon_to_ppi_file_path, 'r') as file:
        for line in file:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split('\t')

            if not (taxon_tag in parts[9] and taxon_tag in parts[10]):
                continue
            
            id1 = parts[0].replace("uniprotkb:", "")
            id2 = parts[1].replace("uniprotkb:", "")
            
            id1 = updated_uniprot_dict.get(id1, id1)
            id2 = updated_uniprot_dict.get(id2, id2)
            
            if not all(id in gene_id_mapping for id in [id1, id2]):  # No mapping, no orthology
                continue
            yield gene_id_mapping[id1], gene_id_mapping[id2]
            
            
            
def get_network_from_edges(edges):
    G = nx.Graph()
    for edge in edges:
        G.add_edge(*edge)
    return G


def taxon_specific_gene_map(all_uniprot_gene_file, taxon_id, db_id):
    """Return a uniprot_id → gene_id mapping for a given taxon and database (e.g. RGD, MGI) from the cached TSV."""
    mapping = dict()
    with open(all_uniprot_gene_file, 'r') as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            parts = line.split('\t')
            if parts[0] != taxon_id:
                if parts[0] == "":
                    if db_id == "Xenbase": # Xenbase two species, so we need to check the taxon id
                        continue
                else:
                    continue
            
            if parts[2] != db_id:
                continue

            uniprot_id = parts[1]
            gene_id = parts[3]
            mapping[uniprot_id] = gene_id
    
    return mapping
                


def update_homology_data(homology_file, ncbi_gene_info_file, stdout=sys.stdout):
    """Populate OrthologInteraction and their species links for all configured taxons."""
    def log(msg: str, **kwargs) -> None:
        print(msg, file=stdout, **kwargs)

    pairs = (
        Interaction.objects
        .select_related("protein_1__gene", "protein_2__gene")
        .values_list(
            "protein_1__gene__entrez_id",
            "protein_2__gene__entrez_id",
        ).distinct()
    )
    
    uni_prot_mod_map_file = "data/species/uniprot_to_orthology.tsv"
    url = "https://ftp.uniprot.org/pub/databases/uniprot/current_release/knowledgebase/idmapping/idmapping.dat.gz"
    if not os.path.exists(uni_prot_mod_map_file):
        log("Downloading UniProt ID mapping (this takes a while)...")
        write_all_mapping(url, uni_prot_mod_map_file) # Takes long time

    log("Loading NCBI gene info...")
    hgnc_to_entrez_dict, entrez_to_hgnc_dict = hgnc_id_to_entrez_id(ncbi_gene_info_file)
    log(f"  {len(hgnc_to_entrez_dict):,} HGNC genes mapped")

    log("Loading orthology data...")
    onthology_data = read_othology_data(homology_file, hgnc_to_entrez_dict)
    log(f"  {len(onthology_data):,} human genes with orthologs")

    log("Loading UniProt update map...")
    updated_uniprot_dict = get_updated_uniprot_dict("data/sec_ac.txt")
    log(f"  {len(updated_uniprot_dict):,} ID updates")

    log("Building gene PK map...")
    gene_pk_map: dict[int, int] = dict(Gene.objects.values_list("entrez_id", "pk"))
    log(f"  {len(gene_pk_map):,} genes indexed")


    n_taxons = len(TAXONS)
    for i, taxon in enumerate(TAXONS):
        taxon_id = taxon
        identifier_prefix = TAXONS[taxon][0]
        taxon_name = TAXONS[taxon][2]
        log(f"\n[{i + 1}/{n_taxons}] {taxon_name} ({identifier_prefix})")

        taxon_specific_gene_mapping = taxon_specific_gene_map(uni_prot_mod_map_file, taxon_id, identifier_prefix)
        log(f"  UniProt mapping: {len(taxon_specific_gene_mapping):,} entries")

        taxon_to_ppi_file_path = f"data/species/{TAXONS[taxon][1]}"
        edges = get_species_edges(taxon_to_ppi_file_path, taxon_id, taxon_specific_gene_mapping, updated_uniprot_dict)
        G = get_network_from_edges(edges)
        log(f"  PPI network: {G.number_of_nodes():,} nodes, {G.number_of_edges():,} edges")

        current_species, _ = Species.objects.get_or_create(name=taxon_name, NCBI_tax_id=int(taxon_id))

        orthointeractions_to_create: dict[tuple[str, str], list[OrthologInteraction, list[Species]]] = {}
        n_pairs = 0
        for pair in pairs:
            n_pairs += 1
            if n_pairs % 5_000 == 0:
                print(f"\r  Pairs scanned: {n_pairs:,}  interactions found: {len(orthointeractions_to_create):,}", end="", flush=True)
            ortho_gene_1 = onthology_data.get(
                entrez_to_hgnc_dict.get(str(pair[0])), False)
            ortho_gene_2 = onthology_data.get(
                entrez_to_hgnc_dict.get(str(pair[1])), False)
            if not ortho_gene_1 or not ortho_gene_2:
                continue
            ortho_gene_1 = [gene for gene in ortho_gene_1 if gene[1] == taxon_id]
            ortho_gene_2 = [gene for gene in ortho_gene_2 if gene[1] == taxon_id]
            
            added = False
            for gene_1 in ortho_gene_1:
                for gene_2 in ortho_gene_2:
                    if G.has_edge(gene_1[0], gene_2[0]):
                        pk1 = gene_pk_map.get(pair[0])
                        pk2 = gene_pk_map.get(pair[1])
                        if pk1 is None or pk2 is None:
                            continue
                        gene_pair_id = tuple(sorted((pk1, pk2)))
                        if gene_pair_id not in orthointeractions_to_create:
                            orthointeractions_to_create[gene_pair_id] = [
                                OrthologInteraction(gene_1_id=gene_pair_id[0], gene_2_id=gene_pair_id[1]), {current_species}]
                        else:
                            orthointeractions_to_create[gene_pair_id][1].add(current_species)
                        added = True                
                    if added:
                        break  
                if added:
                    break
        
        print(f"\r  Pairs scanned: {n_pairs:,}  interactions found: {len(orthointeractions_to_create):,}    ")

        oi_objs = [oi for oi, _ in orthointeractions_to_create.values()] # oioi oi
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
        parser.add_argument("--homology_file", type=str, help="Path to genome alliance orthology file")
        parser.add_argument("--ncbi_gene_info_file", type=str, help="Path to NCBI human gene_info file")

    def handle(self, *args, **options):
        update_homology_data(options["homology_file"], options["ncbi_gene_info_file"], stdout=self.stdout)
