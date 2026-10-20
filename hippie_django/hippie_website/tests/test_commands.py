import gzip
import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import CommandError, call_command
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from ..models import (
    Gene,
    GeneTissue,
    InteractionType,
    OrthologInteraction,
    Source,
    Species,
    Tissue,
)
from .factories import make_interaction, make_protein


class UpdateTissueDataCommandTest(TestCase):
    def test_missing_input_file_raises(self):
        # Paths now default from data/sources.json (they are no longer required
        # flags), so a path that resolves to a missing file must raise rather
        # than run silently.
        with self.assertRaises(CommandError):
            call_command(
                "update_tissue_data",
                gct_path="/nonexistent/does_not_exist.gct",
            )

    def test_existing_gene_tissue_median_is_updated(self):
        gene = Gene.objects.create(entrez_id=101, entrez_name="GENE1")
        tissue = Tissue.objects.create(name="Liver")
        gene_tissue = GeneTissue.objects.create(
            gene=gene, tissue=tissue, median_tpm=2.0
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            gct_path = tmp_path / "test.gct"
            gct_path.write_text(
                "\n".join(
                    [
                        "#1.2",
                        "1\t1",
                        "Name\tDescription\tS1",
                        "ENSG000001.1\tGENE1\t5",
                    ]
                ),
                encoding="utf-8",
            )

            annotation_path = tmp_path / "samples.txt"
            annotation_path.write_text(
                "\n".join(
                    [
                        "sample\tcol1\tcol2\tcol3\tcol4\tcol5\ttissue",
                        "S1\t-\t-\t-\t-\t-\tLiver",
                    ]
                ),
                encoding="utf-8",
            )

            entrez_path = tmp_path / "Homo_sapiens.gene_info"
            entrez_path.write_text(
                "\n".join(
                    [
                        "tax_id\tGeneID\tSymbol\tLocusTag\tSynonyms\tdbXrefs",
                        "9606\t101\tGENE1\t-\t-\tEnsembl:ENSG000001",
                    ]
                ),
                encoding="utf-8",
            )

            call_command(
                "update_tissue_data",
                gct_path=str(gct_path),
                annotation_sample_path=str(annotation_path),
                entrez_homo_path=str(entrez_path),
            )

        gene_tissue.refresh_from_db()
        self.assertEqual(gene_tissue.median_tpm, 5.0)


# ---------------------------------------------------------------------------
# Source homepage URLs + per-pair evidence links
# ---------------------------------------------------------------------------


class MitabCvNameParsingTest(TestCase):
    """Regression test: PSI-MI CV names with embedded parens/quotes.

    IntAct's raw MITAB field for MI:0095 is
    psi-mi:"MI:0095"("proteinchip(r) on a surface-enhanced laser
    desorption/ionization"). The old `field_val.split("(")[-1].rstrip(")")`
    logic split on the LAST "(" (inside the name's own "(r)") instead of
    the first, and only stripped trailing ")" chars, producing the
    corrupted 'r) on a surface-enhanced laser desorption/ionization"'.
    """

    def test_parse_tech_handles_embedded_parens_and_quotes(self):
        from ..management.commands.hippie_update import _parse_tech

        field = (
            'psi-mi:"MI:0095"("proteinchip(r) on a surface-enhanced '
            'laser desorption/ionization")'
        )
        result, skip = _parse_tech(field)
        self.assertFalse(skip)
        assert result is not None
        mi_code, name = result
        self.assertEqual(mi_code, "MI:0095")
        self.assertEqual(
            name, "proteinchip(r) on a surface-enhanced laser desorption/ionization"
        )

    def test_parse_tech_plain_name_unaffected(self):
        from ..management.commands.hippie_update import _parse_tech

        result, skip = _parse_tech('psi-mi:"MI:0018"(two hybrid)')
        self.assertFalse(skip)
        assert result is not None
        _, name = result
        self.assertEqual(name, "Two-hybrid")  # normalized via _TECH_NORM

    def test_parse_interaction_type_handles_embedded_parens(self):
        from ..management.commands.hippie_update import _parse_interaction_type

        itype = _parse_interaction_type(
            'psi-mi:"MI:0095"("proteinchip(r) on a surface-enhanced '
            'laser desorption/ionization")'
        )
        self.assertEqual(
            itype, "proteinchip(r) on a surface-enhanced laser desorption/ionization"
        )

    def test_extract_mi_code(self):
        from ..management.commands.hippie_update import _extract_mi_code

        self.assertEqual(
            _extract_mi_code('psi-mi:"MI:0915"(physical association)'), "MI:0915"
        )
        self.assertEqual(_extract_mi_code("physical association"), "")

    def test_parse_source_handles_embedded_parens(self):
        from ..management.commands.hippie_update import _parse_source

        source = _parse_source(
            'psi-mi:"MI:0095"("proteinchip(r) on a surface-enhanced '
            'laser desorption/ionization")'
        )
        self.assertEqual(
            source, "proteinchip(r) on a surface-enhanced laser desorption/ionization"
        )


# ---------------------------------------------------------------------------
# export_downloads — public release files
# ---------------------------------------------------------------------------


class ExportDownloadsCommandTest(TestCase):
    """End-to-end smoke test for the download export.

    The command streams every Interaction through ``prefetch_related``; a stale
    lookup there (e.g. a field removed by a migration) only blows up at runtime
    against a real database, so run it for real over a tiny fixture.
    """

    @classmethod
    def setUpTestData(cls):
        cls.p1 = make_protein(
            "BRCA1", uniprot_name="BRCA1_HUMAN", gene_id=672, accession="P38398"
        )
        cls.p2 = make_protein(
            "TP53", uniprot_name="P53_HUMAN", gene_id=7157, accession="P04637"
        )
        inter = make_interaction(cls.p1, cls.p2, score=0.9)
        inter.sources.add(
            Source.objects.create(name="biogrid"),
            Source.objects.create(name="no-such-db"),
        )
        inter.interaction_types.add(
            InteractionType.objects.create(
                name="physical association", psi_mi_code="MI:0915"
            )
        )

        # Conserved species live on OrthologInteraction, keyed on the gene pair.
        g1, g2 = cls.p1.gene, cls.p2.gene
        lo, hi = (g1, g2) if g1.pk <= g2.pk else (g2, g1)
        ortholog = OrthologInteraction.objects.create(gene_1=lo, gene_2=hi)
        mouse = Species.objects.create(name="Mus musculus", NCBI_tax_id=10090)
        ortholog.ortholog_species.add(mouse)

    def _run(self):
        tmpdir = tempfile.mkdtemp()
        call_command("export_downloads", tmpdir, stdout=StringIO())
        return Path(tmpdir)

    def test_writes_all_three_files(self):
        out = self._run()
        for name in (
            "HIPPIE-current.mitab.txt.gz",
            "HIPPIE-current.txt.gz",
            "HIPPIE-current.stats.txt.gz",
        ):
            self.assertTrue((out / name).exists(), f"{name} missing")

    def test_mitab_row_carries_conserved_species(self):
        out = self._run()
        with gzip.open(out / "HIPPIE-current.mitab.txt.gz", "rt") as f:
            header, row = f.read().splitlines()[:2]
        cols = row.split("\t")
        self.assertEqual(len(cols), len(header.split("\t")))
        # "Presence In Other Species" is the 16th MITAB column (index 15).
        self.assertEqual(cols[15], "taxid:10090(Mus musculus)")

    def test_mitab_cv_columns_are_psi_mi_terms(self):
        out = self._run()
        with gzip.open(out / "HIPPIE-current.mitab.txt.gz", "rt") as f:
            cols = f.read().splitlines()[1].split("\t")
        self.assertEqual(cols[11], 'psi-mi:"MI:0915"(physical association)')
        # A source without a registry code falls back to psi-mi:<name>.
        self.assertEqual(
            sorted(cols[12].split("|")),
            ['psi-mi:"MI:0463"(biogrid)', "psi-mi:no-such-db"],
        )

    def test_species_lookup_does_not_scale_with_interaction_count(self):
        """The ortholog map is loaded once, not once per exported row."""
        with CaptureQueriesContext(connection) as ctx:
            self._run()
        baseline = len(ctx.captured_queries)

        for i in range(5):
            extra = make_protein(f"X{i}", gene_id=90000 + i, accession=f"Q0000{i}")
            make_interaction(self.p1, extra, score=0.5)

        with CaptureQueriesContext(connection) as ctx:
            self._run()
        self.assertEqual(len(ctx.captured_queries), baseline)


# ---------------------------------------------------------------------------
# ML Splits — orphan-aware stats, filter-aware medians, accession-based CSVs
# ---------------------------------------------------------------------------


class BackfillTypeCodesCommandTest(TestCase):
    def test_fills_only_empty_codes_from_column_12(self):
        InteractionType.objects.create(name="physical association")
        InteractionType.objects.create(name="direct interaction", psi_mi_code="MI:0407")
        InteractionType.objects.create(name="unseen type")
        cols = ["-"] * 15
        cols[11] = 'psi-mi:"MI:0915"(physical association)'
        other = list(cols)
        other[11] = 'psi-mi:"MI:9999"(direct interaction)'
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "mitab.txt"
            path.write_text("\t".join(cols) + "\n" + "\t".join(other) + "\n")
            out = StringIO()
            call_command("backfill_type_codes", str(path), stdout=out)

        codes = dict(InteractionType.objects.values_list("name", "psi_mi_code"))
        self.assertEqual(codes["physical association"], "MI:0915")
        self.assertEqual(codes["direct interaction"], "MI:0407")  # not overwritten
        self.assertEqual(codes["unseen type"], "")
        self.assertIn("unseen type", out.getvalue())


class AddSplitSequencesCommandTest(TestCase):
    def test_appends_the_split_subset_once(self):
        import zipfile

        from ..services.nextflow_runner import OUTPUT_FILES

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            (base / "data" / "precomputed").mkdir(parents=True)
            (base / "data" / "precomputed" / "sequences.fasta").write_text(
                ">P1\nMKV\n>P2\nMAA\n>P9\nMZZ\n"
            )
            archive = base / "HIPPIE-HC-HRNI3.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                for name in OUTPUT_FILES:
                    zf.writestr(name, "protein1,protein2,label\nP1,P2,1\n")

            with self.settings(BASE_DIR=base):
                call_command("add_split_sequences", str(base), stdout=StringIO())
                out = StringIO()
                call_command("add_split_sequences", str(base), stdout=out)

            with zipfile.ZipFile(archive) as zf:
                self.assertEqual(zf.namelist().count("sequences.fasta"), 1)
                self.assertEqual(
                    zf.read("sequences.fasta").decode(), ">P1\nMKV\n>P2\nMAA\n"
                )
        self.assertIn("already has sequences.fasta", out.getvalue())
