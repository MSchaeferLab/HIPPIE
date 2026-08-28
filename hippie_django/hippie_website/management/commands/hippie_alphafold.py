"""
Create a HIPPIE PPI pairs table with AlphaFold dimer-prediction metadata.
Inputs:
  Interaction table (database) HIPPIE PPI pairs, same rows as make_export's HIPPIE-current.txt output
  homodimer_metadata.csv  AF predictions for single-protein homodimers https://ftp.ebi.ac.uk/pub/databases/alphafold/
  heterodimer_metadata.csv AF predictions for two-protein complexes https://ftp.ebi.ac.uk/pub/databases/alphafold/

Mapping logic:
  - HIPPIE pairs where A == B are self-interactions -> matched against
    homodimer_metadata (human only, taxId 9606) by uniprot accession.
  - HIPPIE pairs where A != B are matched against heterodimer_metadata by first
    mapping homomer modelEntityIds to UniProt accessions
  - Accessions are matched exactly, isoform suffix included. A HIPPIE isoform entry only
    matches an AF prediction actually made for that isoform, never the
    canonical accession's prediction, and vice versa.
  - Where a HIPPIE pair has multiple AF predictions, only the single best one
    max ipSAE_max is kept, so every HIPPIE row
  - Every HIPPIE row is preserved; unmatched AF columns are "NA".

Outputs:
  hippie_with_ipSAE_max.tsv   original HIPPIE columns + AlphaFold modelEntityId,
                               ipSAE_max, pDockQ2_max
  hippie_alphafold_full.tsv   HIPPIE id/score columns + every available AF
                               metadata column (union of homodimer + heterodimer
                               schemas; NA where a column doesn't apply/exist)
Also prints a coverage summary (HIPPIE entries matched to an AF prediction,
split by homo/heterodimer) to stdout

Usage:
  python manage.py hippie_alphafold homodimer_metadata.csv heterodimer_metadata.csv --out ./hippie_af
"""
import csv
from django.core.management.base import BaseCommand
from hippie_website.models import Interaction

NA = "NA"


def ipsae_max(row):
    return max(float(row["ipSAE_AB"]), float(row["ipSAE_BA"]))


def pdockq2_max(row, dimer_type):
    """dimer_type 'homo': homodimer_metadata.csv has no precomputed max, so
    compute it from pDockQ2_AB/BA. dimer_type 'hetero': heterodimer_metadata.csv
    already reports the max as max_pDockQ2_AB, so use it as-is."""
    if dimer_type == "homo":
        return max(float(row["pDockQ2_AB"]), float(row["pDockQ2_BA"]))
    return float(row["max_pDockQ2_AB"])


def hippie_rows():
    """Yield the same rows make_export's HIPPIE-current.txt contains
    (uniprot_accession_A, uniprot_name_A, entrez_A, uniprot_accession_B,
    uniprot_name_B, entrez_B, score, info), read live from the Interaction
    table instead of the flat file."""
    qs = Interaction.objects.select_related(
        "protein_1__gene", "protein_2__gene"
    ).prefetch_related("experiments", "publications", "sources").order_by("pk")
    for inter in qs.iterator(chunk_size=10_000):
        a, b = inter.protein_1, inter.protein_2
        info = (
            "experiments:" + ",".join(e.name for e in inter.experiments.all())
            + ";pmids:" + ",".join(str(pub.pmid) for pub in inter.publications.all())
            + ";sources:" + ",".join(s.name for s in inter.sources.all())
        )
        yield {
            "uniprot_accession_A": a.uniprot_accession,
            "uniprot_name_A": a.uniprot_name,
            "entrez_A": str(a.gene.entrez_id),
            "uniprot_accession_B": b.uniprot_accession,
            "uniprot_name_B": b.uniprot_name,
            "entrez_B": str(b.gene.entrez_id),
            "score": f"{inter.score:.2f}",
            "info": info,
        }


class Command(BaseCommand):
    help = "Create a HIPPIE PPI pairs table with AlphaFold dimer-prediction metadata."

    def add_arguments(self, parser):
        parser.add_argument("homodimer_metadata", help="Path to homodimer_metadata.csv")
        parser.add_argument("heterodimer_metadata", help="Path to heterodimer_metadata.csv")
        parser.add_argument("--out", default=".", help="Output directory (default: current directory)")

    def handle(self, *args, **options):
        homodimer_metadata = options["homodimer_metadata"]
        heterodimer_metadata = options["heterodimer_metadata"]
        OUT = options["out"]

        #  homodimer_metadata.csv filter forhuman only and create a dictionary modelEntityId to accession,
        #  accession -to best (max ipSAE_max) if multiple predictions, max ipSAE_max is kept
        model_to_acc = {}
        homo_best = {}      # acc -> row dict
        homo_n_pred = {}     # acc -> number of candidate predictions seen

        with open(homodimer_metadata, newline="") as f:
            for row in csv.DictReader(f):
                if row["taxId"] != "9606":
                    continue
                acc = row["uniprotAccession"]
                model_to_acc[row["modelEntityId"]] = acc
                homo_n_pred[acc] = homo_n_pred.get(acc, 0) + 1
                if acc not in homo_best or ipsae_max(row) > ipsae_max(homo_best[acc]):
                    homo_best[acc] = row

        print(f"homodimer_metadata.csv (human): {len(model_to_acc):,} model entities, "
              f"{len(homo_best):,} distinct accessions")

        #  heterodimer_metadata.csv: resolve homomer pair to accession pair,
        # --- best (max ipSAE_max) is kept in the case of multiple prediction--
        het_best = {}        # (accA, accB) sorted tuple -> row dict (+ accA/accB)
        het_n_pred = {}       # pair -> number of candidate predictions seen

        with open(heterodimer_metadata, newline="") as f:
            for row in csv.DictReader(f):
                m1, m2 = (x.strip().replace("_", "-") for x in row["homomers"].strip("[]").split(","))
                acc1, acc2 = model_to_acc.get(m1), model_to_acc.get(m2)
                if acc1 is None or acc2 is None or acc1 == acc2:
                    continue
                pair = (acc1, acc2) if acc1 < acc2 else (acc2, acc1)
                het_n_pred[pair] = het_n_pred.get(pair, 0) + 1
                if pair not in het_best or ipsae_max(row) > ipsae_max(het_best[pair]):
                    row = dict(row)
                    row["accA"], row["accB"] = pair
                    het_best[pair] = row

        print(f"heterodimer_metadata.csv: {len(het_best):,} distinct human accession pairs")

        for label, n_pred in [("heterodimer: predictions per resolved pair", het_n_pred),
                               ("homodimer: predictions per protein", homo_n_pred)]:
            hist = {}
            for n in n_pred.values():
                hist[n] = hist.get(n, 0) + 1
            print(f"{label}: " + ", ".join(f"{n}->{c:,}" for n, c in sorted(hist.items())[:5]))

        # -column layout for the "full" output: union of both AF3 schemas --
        with open(heterodimer_metadata, newline="") as f:
            het_columns = [c for c in next(csv.reader(f)) if c != "homomers"]
        with open(homodimer_metadata, newline="") as f:
            homo_columns = next(csv.reader(f))
        homo_extra_columns = [c for c in homo_columns if c not in het_columns]
        af_columns = het_columns + homo_extra_columns + ["ipSAE_max", "pDockQ2_max"]

        id_columns = ["uniprot_accession_A", "uniprot_name_A", "entrez_A",
                      "uniprot_accession_B", "uniprot_name_B", "entrez_B", "score"]

        def af_row_values(row, dimer_type):
            """Build the full-width AF metadata row (af_columns order) for a matched row."""
            if row is None:
                return [NA] * (1 + len(af_columns))
            values = [dimer_type]
            for col in het_columns + homo_extra_columns:
                values.append(row.get(col, NA))
            values.append(f"{ipsae_max(row):g}")
            values.append(f"{pdockq2_max(row, dimer_type):g}")
            return values

        #HIPPIE, match, write both outputs in one pass
        homo_total = het_total = 0
        homo_matched = het_matched = 0
        homo_ge06 = het_ge06 = 0

        with open(f"{OUT}/hippie_with_ipSAE_max.tsv", "w", newline="") as f_slim, \
             open(f"{OUT}/hippie_alphafold_full.tsv", "w", newline="") as f_full:

            reader = hippie_rows()
            w_slim = csv.writer(f_slim, delimiter="\t")
            w_full = csv.writer(f_full, delimiter="\t")
            w_slim.writerow(id_columns + ["info", "modelEntityId", "ipSAE_max", "pDockQ2_max"])
            w_full.writerow(id_columns + ["dimer_type"] + af_columns)

            for row in reader:
                acc_a, acc_b = row["uniprot_accession_A"], row["uniprot_accession_B"]
                ids = [acc_a, row["uniprot_name_A"], row["entrez_A"],
                       acc_b, row["uniprot_name_B"], row["entrez_B"], row["score"]]

                if acc_a == acc_b:
                    homo_total += 1
                    match, dimer_type = homo_best.get(acc_a), "homo"
                else:
                    het_total += 1
                    pair = (acc_a, acc_b) if acc_a < acc_b else (acc_b, acc_a)
                    match, dimer_type = het_best.get(pair), "hetero"

                ipsae = ipsae_max(match) if match is not None else None
                pdockq2 = pdockq2_max(match, dimer_type) if match is not None else None
                model_id = match["modelEntityId"] if match is not None else NA
                w_slim.writerow(ids + [row["info"], model_id,
                                        f"{ipsae:g}" if ipsae is not None else NA,
                                        f"{pdockq2:g}" if pdockq2 is not None else NA])
                w_full.writerow(ids + af_row_values(match, dimer_type if match is not None else NA))

                if match is not None:
                    if acc_a == acc_b:
                        homo_matched += 1
                        homo_ge06 += ipsae >= 0.6
                    else:
                        het_matched += 1
                        het_ge06 += ipsae >= 0.6

        n_total, n_matched = homo_total + het_total, homo_matched + het_matched
        n_ge06 = homo_ge06 + het_ge06

        print(f"\nHIPPIE entries total:       {n_total:>10,}")
        print(f"  homodimer  (A == B):      {homo_total:>10,}   matched to AF3: {homo_matched:>8,} "
              f"({100*homo_matched/homo_total:5.2f}%)   ipSAE_max>=0.6: {homo_ge06:,}")
        print(f"  heterodimer (A != B):     {het_total:>10,}   matched to AF3: {het_matched:>8,} "
              f"({100*het_matched/het_total:5.2f}%)   ipSAE_max>=0.6: {het_ge06:,}")
        print(f"  total matched:            {n_matched:>10,} / {n_total:,} ({100*n_matched/n_total:5.2f}%)")
        print(f"  total ipSAE_max>=0.6:     {n_ge06:>10,}")
        print(f"wrote {OUT}/hippie_with_ipSAE_max.tsv")
        print(f"wrote {OUT}/hippie_alphafold_full.tsv")
