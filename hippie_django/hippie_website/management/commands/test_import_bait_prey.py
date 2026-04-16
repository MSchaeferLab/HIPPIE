
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from hippie_website.models import (
    BaitPreyTest, BaitPreyAssociation,
    ExperimentType, Interaction, Protein,
    ProteinEntrez, ProteinUniProt, UniProtAccession,
)

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

DEFAULT_DATA_FILE = Path(__file__).resolve().parents[3] / "data" / "dummy-MI-0006.csv"

NCBI_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
NCBI_ESUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
UNIPROT_SEARCH = "https://rest.uniprot.org/uniprotkb/search"

NCBI_RATE_LIMIT_SLEEP = 0.4  # stay within NCBI's 3 req/s limit (no API key)
BATCH_SIZE = 100              # gene names per external API call

PSI_MI_CODE_MAP = {
    "MI-0006": "MI:0004",
}


def _get_json(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _fetch_entrez_batch(gene_names):
    """
    Batch-fetch Entrez gene data for a list of gene symbols.
    Returns {gene_name: (gene_id, official_symbol)} for every hit found.
    Unmatched names are silently omitted.
    """
    term = (
        "(" + " OR ".join(f"{n}[Gene Name]" for n in gene_names) + ")"
        " AND 9606[Taxonomy ID]"
    )
    search_url = NCBI_ESEARCH + "?" + urllib.parse.urlencode({
        "db": "gene",
        "term": term,
        "retmode": "json",
        "retmax": str(len(gene_names) * 2),  # allow room for aliases
    })
    try:
        ids = _get_json(search_url)["esearchresult"]["idlist"]
        if not ids:
            return {}
        time.sleep(NCBI_RATE_LIMIT_SLEEP)
        summary_url = NCBI_ESUMMARY + "?" + urllib.parse.urlencode({
            "db": "gene",
            "id": ",".join(ids),
            "retmode": "json",
        })
        result = _get_json(summary_url)["result"]
    except (urllib.error.URLError, KeyError, ValueError):
        return {}

    lookup = {n.upper(): n for n in gene_names}
    mapping = {}
    for gene_id_str, summary in result.items():
        if gene_id_str == "uids":
            continue
        symbol = summary.get("name", "")
        original = lookup.get(symbol.upper())
        if original and original not in mapping:
            mapping[original] = (int(gene_id_str), symbol)
    return mapping


def _fetch_uniprot_batch(gene_names):
    """
    Batch-fetch UniProt data for a list of gene symbols.
    Returns {gene_name: (accession, entry_id)} for every reviewed human hit.
    Unmatched names are silently omitted.
    """
    gene_list = " OR ".join(f"gene_exact:{n}" for n in gene_names)
    url = UNIPROT_SEARCH + "?" + urllib.parse.urlencode({
        "query": f"({gene_list}) AND organism_id:9606 AND reviewed:true",
        "fields": "accession,id,gene_names",
        "format": "json",
        "size": str(len(gene_names)),
    })
    try:
        results = _get_json(url).get("results", [])
    except (urllib.error.URLError, KeyError):
        return {}

    lookup = {n.upper(): n for n in gene_names}
    mapping = {}
    for hit in results:
        accession = hit.get("primaryAccession")
        entry_id = hit.get("uniProtkbId")
        if not accession or not entry_id:
            continue
        for gene in hit.get("genes", []):
            symbol = gene.get("geneName", {}).get("value", "")
            original = lookup.get(symbol.upper())
            if original and original not in mapping:
                mapping[original] = (accession, entry_id)
                break
    return mapping


class Command(BaseCommand):
    help = "Import bait-prey test data from a tab-separated CSV file."

    def add_arguments(self, parser):
        parser.add_argument(
            "--file",
            type=str,
            default=str(DEFAULT_DATA_FILE),
            help="Path to the input TSV file (default: data/test_MI-0006.csv).",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        input_file = Path(options["file"])
        if not input_file.exists():
            raise CommandError(f"Input file not found: {input_file}")

        # ------------------------------------------------------------------
        # Pass 1: parse file, collect all rows and unique gene names
        # ------------------------------------------------------------------
        rows = []
        with open(input_file, "r") as f:
            for line in f:
                if line.startswith("gene_name_bait"):
                    continue
                fields = line.strip().split("\t")
                if len(fields) < 6:
                    continue
                rows.append({
                    "bait":        fields[0],
                    "prey":        fields[1],
                    "detection":   bool(int(fields[3])),
                    "method_code": PSI_MI_CODE_MAP.get(fields[4], fields[4]),
                    "pmid":        int(fields[5]),
                })

        self.stdout.write(f"Parsed {len(rows)} rows from {input_file}.")

        all_gene_names = {r["bait"] for r in rows} | {r["prey"] for r in rows}

        # ------------------------------------------------------------------
        # Pass 2: create Protein rows, batch-enrich only the new ones
        # ------------------------------------------------------------------
        existing = set(
            Protein.objects.filter(name__in=all_gene_names)
            .values_list("name", flat=True)
        )
        new_names = all_gene_names - existing
        for name in new_names:
            Protein.objects.get_or_create(name=name)

        if new_names:
            self.stdout.write(
                f"Fetching external data for {len(new_names)} new proteins …"
            )
            self._enrich_proteins(
                new_names, ProteinEntrez, ProteinUniProt, UniProtAccession
            )

        # Name → Protein cache so pass 3 makes no per-row DB lookups
        proteins = {
            p.name: p
            for p in Protein.objects.filter(name__in=all_gene_names)
        }

        # ------------------------------------------------------------------
        # Pass 3: create interactions and bait-prey records
        # ------------------------------------------------------------------
        # Cache ExperimentType lookups — all rows share the same method code
        method_cache = {}
        imported = skipped = 0

        for row in rows:
            method_code = row["method_code"]
            if method_code not in method_cache:
                try:
                    method_cache[method_code] = ExperimentType.objects.get(
                        psi_mi_code=method_code
                    )
                except ExperimentType.DoesNotExist:
                    raise CommandError(
                        f"ExperimentType with PSI-MI code '{method_code}' not found. "
                        "Ensure all methods are pre-populated (e.g. via seed_test_data)."
                    )
            method = method_cache[method_code]

            bait_protein = proteins[row["bait"]]
            prey_protein = proteins[row["prey"]]

            # Enforce canonical order: protein_1_id <= protein_2_id
            if bait_protein.pk <= prey_protein.pk:
                protein_1, protein_2 = bait_protein, prey_protein
                direction = BaitPreyAssociation.Directions.PROTEIN_ONE_BAIT
            else:
                protein_1, protein_2 = prey_protein, bait_protein
                direction = BaitPreyAssociation.Directions.PROTEIN_TWO_BAIT

            # score=0.0 is a placeholder; recalculate after all evidence is imported
            interaction, _ = Interaction.objects.get_or_create(
                protein_1=protein_1,
                protein_2=protein_2,
                defaults={"score": 0.0},
            )
            bpa, _ = BaitPreyAssociation.objects.get_or_create(
                interaction=interaction,
                direction=direction,
            )
            bpt, _ = BaitPreyTest.objects.get_or_create(
                pmid=row["pmid"],
                method=method,
                detection=row["detection"],
            )
            bpa.tests_performed.add(bpt)
            imported += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. {imported} rows imported, {skipped} rows skipped."
            )
        )

    def _enrich_proteins(self, gene_names, ProteinEntrez, ProteinUniProt, UniProtAccession):
        """Batch-fetch Entrez and UniProt data for a set of newly created proteins."""
        names = list(gene_names)
        for i in range(0, len(names), BATCH_SIZE):
            chunk = names[i : i + BATCH_SIZE]
            batch_num = i // BATCH_SIZE + 1
            total_batches = (len(names) + BATCH_SIZE - 1) // BATCH_SIZE

            self.stdout.write(
                f"  Entrez  batch {batch_num}/{total_batches} ({len(chunk)} genes) …"
            )
            entrez_data = _fetch_entrez_batch(chunk)
            time.sleep(NCBI_RATE_LIMIT_SLEEP)

            self.stdout.write(
                f"  UniProt batch {batch_num}/{total_batches} ({len(chunk)} genes) …"
            )
            uniprot_data = _fetch_uniprot_batch(chunk)

            proteins = {
                p.name: p
                for p in Protein.objects.filter(name__in=chunk)
            }

            for name in chunk:
                protein = proteins.get(name)
                if not protein:
                    continue

                if name in entrez_data:
                    gene_id, symbol = entrez_data[name]
                    ProteinEntrez.objects.get_or_create(
                        protein=protein,
                        gene_id=gene_id,
                        defaults={"name": symbol or name},
                    )
                else:
                    self.stderr.write(
                        f"  Warning: no Entrez entry found for '{name}'"
                    )

                if name in uniprot_data:
                    accession, entry_id = uniprot_data[name]
                    ProteinUniProt.objects.get_or_create(
                        protein=protein,
                        uniprot_id=entry_id,
                        defaults={"version": 1},
                    )
                    UniProtAccession.objects.get_or_create(
                        accession=accession,
                        uniprot_id=entry_id,
                    )
                else:
                    self.stderr.write(
                        f"  Warning: no UniProt entry found for '{name}'"
                    )
