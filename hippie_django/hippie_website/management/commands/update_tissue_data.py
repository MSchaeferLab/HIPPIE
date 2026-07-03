from pathlib import Path

import numpy as np
from django.core.management.base import BaseCommand, CommandError

from hippie_website.models import Gene, GeneTissue, Tissue

MIN_MEDIAN_RPKM = 1.0


def _parse_header(header_line: str, sample_to_verbose: dict[str, str]) -> dict:
    samples = header_line.strip().split("\t")[2:]
    tissue_dict: dict = dict()
    for idx, sample in enumerate(samples):
        tissue_verbose = sample_to_verbose[sample]
        if tissue_verbose in tissue_dict:
            tissue_dict[tissue_verbose]["idx"].append(idx)
        else:
            tissue_dict[tissue_verbose] = {"idx": [idx]}

    return tissue_dict


def _get_ensembl_entrez_map(path_homo_entrez: Path) -> dict[str, tuple[int, str]]:
    map_dict: dict[str, tuple[int, str]] = dict()
    with path_homo_entrez.open(newline="", encoding="utf-8") as f:
        next(f)  # skip header
        for line in f:
            parts = line.split("\t")
            gene_id = int(parts[1])
            gene_name = parts[2]
            dbXref = parts[5]
            found = False
            ensembl_id = ""
            for xref in dbXref.split("|"):
                if xref.startswith("Ensembl:"):
                    ensembl_id = xref.split(":")[1]
                    found = True

            if not found:
                continue
            map_dict[ensembl_id] = (gene_id, gene_name)

    return map_dict


class Command(BaseCommand):
    help = "Update or create Tissue and expression from GTEx."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--gct-path",
            help="Path to the GTEx gct file",
            required=True,
        )
        parser.add_argument(
            "--entrez-homo-path",
            default=str(
                Path(__file__).resolve().parents[3] / "data" / "Homo_sapiens.gene_info"
            ),
            help="Path to the entrez gene-file",
        )
        parser.add_argument(
            "--annotation-sample-path",
            help="Path to the GTEx sample annotation-file",
            required=True,
        )

    def handle(self, **options) -> None:
        path_cgt = Path(options["gct_path"])
        if not path_cgt.exists():
            raise CommandError(f"File not found: {path_cgt}")

        path_homo_entrez = Path(options["entrez_homo_path"])
        if not path_homo_entrez.exists():
            raise CommandError(f"File not found: {path_homo_entrez}")

        path_annotation_gtex = Path(options["annotation_sample_path"])
        if not path_annotation_gtex.exists():
            raise CommandError(f"File not found: {path_annotation_gtex}")

        # --- Step 1: parse sample annotations ---
        self.stdout.write("Step 1/5  Parsing sample annotations...")
        with path_annotation_gtex.open(newline="", encoding="utf-8") as fa:
            sample_to_verbose: dict[str, str] = dict()
            next(fa)  # skip header
            for line in fa:
                parts = line.split("\t")
                sample = parts[0]
                verbose = parts[6]
                sample_to_verbose[sample] = verbose
        self.stdout.write(
            f"          {len(sample_to_verbose):,} samples mapped to {len(set(sample_to_verbose.values())):,} tissues."
        )

        # --- Step 2: parse GCT expression data ---
        self.stdout.write("Step 2/5  Parsing GTEx GCT file (this may take a while)...")
        genes_read = 0
        with path_cgt.open(newline="", encoding="utf-8") as fh:
            tissue_dict: dict = dict()
            header_found = False
            for line in fh:
                if line.startswith("Name"):
                    header_found = True
                    tissue_dict = _parse_header(line, sample_to_verbose)
                    continue

                if header_found:
                    values = line.split("\t")
                    rpkms = np.array(values[2:], dtype=float)
                    read_name = values[0]
                    if "." in read_name:
                        read_name = read_name.split(".")[0]  # drop version
                    for tissue in tissue_dict:
                        idx = tissue_dict[tissue]["idx"]
                        tissue_dict[tissue][read_name] = np.median(rpkms[idx])
                    genes_read += 1
                    if genes_read % 5000 == 0:
                        self.stdout.write(
                            f"          {genes_read:,} genes processed...", ending="\r"
                        )
        self.stdout.write(
            f"          {genes_read:,} genes processed across {len(tissue_dict):,} tissues."
        )

        # --- Step 3: build Ensembl → Entrez map ---
        self.stdout.write("Step 3/5  Building Ensembl → Entrez ID map...")
        map_dict = _get_ensembl_entrez_map(path_homo_entrez)
        read_ids = {
            gene
            for tissue in tissue_dict
            for gene in tissue_dict[tissue]
            if gene != "idx"
        }
        entrez_genes = [map_dict[rid] for rid in read_ids if rid in map_dict]
        self.stdout.write(
            f"          {len(map_dict):,} Ensembl IDs mapped; {len(entrez_genes):,} match expression data."
        )

        # --- Step 4: sync Gene and Tissue records ---
        self.stdout.write("Step 4/5  Syncing Gene and Tissue records...")
        existing_genes = Gene.objects.filter(entrez_id__in=[e[0] for e in entrez_genes])
        gene_cache: dict[int, Gene] = {g.entrez_id: g for g in existing_genes}
        ## Comment out to only include genes that exists, we dont want to swamp the DB with RNA-genes
        # new_genes = Gene.objects.bulk_create(
        #     [
        #         Gene(entrez_id=eid, entrez_name=name)
        #         for eid, name in entrez_genes
        #         if eid not in gene_cache
        #     ]
        # )
        # gene_cache.update({g.entrez_id: g for g in new_genes})
        self.stdout.write(f"          Genes — {len(existing_genes):,} existing")

        tissues = set(sample_to_verbose.values())
        existing_tissues = Tissue.objects.filter(name__in=tissues)
        tissue_cache: dict[str, Tissue] = {t.name: t for t in existing_tissues}
        new_tissues = Tissue.objects.bulk_create(
            [Tissue(name=t) for t in tissues if t not in tissue_cache]
        )
        tissue_cache.update({t.name: t for t in new_tissues})
        self.stdout.write(
            f"          Tissues — {len(existing_tissues):,} existing, {len(new_tissues):,} created."
        )

        # --- Step 5: sync GeneTissue expression rows ---
        self.stdout.write("Step 5/5  Syncing GeneTissue expression rows...")
        created = 0
        updated = 0
        total_tissues = len(tissue_dict)
        for i, (tissue_name, gene_medians) in enumerate(tissue_dict.items(), 1):
            gene_tissues_to_create = []
            gene_tissues_to_update = []
            existing_gene_tissues = {
                gt.gene_id: gt
                for gt in GeneTissue.objects.filter(tissue=tissue_cache[tissue_name])
            }
            for rid, median in gene_medians.items():
                if rid == "idx" or rid not in map_dict or median < MIN_MEDIAN_RPKM:
                    continue
                eid, _ = map_dict[rid]
                gene = gene_cache.get(eid)
                if not gene:
                    continue
                existing_gene_tissue = existing_gene_tissues.get(gene.pk)
                if existing_gene_tissue is None:
                    gene_tissues_to_create.append(
                        GeneTissue(
                            gene=gene,
                            tissue=tissue_cache[tissue_name],
                            median_rpkm=median,
                        )
                    )
                    continue
                if existing_gene_tissue.median_rpkm != median:
                    existing_gene_tissue.median_rpkm = median
                    gene_tissues_to_update.append(existing_gene_tissue)

            GeneTissue.objects.bulk_create(gene_tissues_to_create)
            GeneTissue.objects.bulk_update(gene_tissues_to_update, ["median_rpkm"])
            created += len(gene_tissues_to_create)
            updated += len(gene_tissues_to_update)
            bar = "#" * i + "-" * (total_tissues - i)
            self.stdout.write(
                f"\r          [{bar}] {i}/{total_tissues} {tissue_name:<40}", ending=""
            )

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"Done — {created:,} GeneTissue rows created, {updated:,} updated."
            )
        )

        # Record GTEx / gene-info source versions on the current release.
        from hippie_website.models import ReleaseMeta

        ReleaseMeta.record_resources(
            {
                "GTEx": path_cgt.name,
                "NCBI Gene": path_homo_entrez.name,
            }
        )
