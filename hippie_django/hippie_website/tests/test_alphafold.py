"""Tests for the AlphaFold DB cross-link: the ``hippie_update --alphafold``
import (threshold filter, either-orientation matching, full-replace clearing)
and the "Further information" link on the detail pages.
"""

from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse

from ..models import Interaction
from .factories import HippieTestCase, make_interaction, make_noninteraction

HEADER = (
    "uniprot_accession_A\tuniprot_name_A\tentrez_A\tuniprot_accession_B\t"
    "uniprot_name_B\tentrez_B\tscore\tinfo\tmodelEntityId\tipSAE_max\tpDockQ2_max\n"
)


def _row(acc_a: str, acc_b: str, model: str, ipsae: str, pdockq2: str) -> str:
    return f"{acc_a}\tA\t1\t{acc_b}\tB\t2\t0.5\tinfo\t{model}\t{ipsae}\t{pdockq2}\n"


class AlphaFoldImportTest(HippieTestCase):
    def _run(self, *rows: str, only: bool = True) -> str:
        out = StringIO()
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "af.tsv"
            path.write_text(HEADER + "".join(rows))
            args = ["--alphafold", str(path)]
            if only:
                args.append("--alphafold-only")
            call_command("hippie_update", *args, stdout=out)
        return out.getvalue()

    def _model_id(self, ix: Interaction) -> str | None:
        ix.refresh_from_db(fields=["alphafold_model_id"])
        return ix.alphafold_model_id

    def test_confident_prediction_is_stored_in_either_orientation(self):
        # File order (TP53, BRCA1) need not match storage order.
        self._run(_row("P04637", "P38398", "AF-0000000000000001", "0.6", "0.23"))
        self.assertEqual(self._model_id(self.ix), "AF-0000000000000001")

    def test_below_either_cutoff_or_na_is_dropped(self):
        ix2 = make_interaction(self.brca1, self.egfr)
        ix3 = make_interaction(self.tp53, self.egfr)
        self._run(
            _row("P38398", "P04637", "AF-1", "0.59", "0.9"),
            _row("P38398", "P00533", "AF-2", "0.9", "0.22"),
            _row("P04637", "P00533", "NA", "NA", "NA"),
        )
        self.assertIsNone(self._model_id(self.ix))
        self.assertIsNone(self._model_id(ix2))
        self.assertIsNone(self._model_id(ix3))

    def test_rerun_clears_stale_ids(self):
        self._run(_row("P38398", "P04637", "AF-1", "0.9", "0.9"))
        self._run(_row("P38398", "P04637", "AF-1", "0.1", "0.9"))
        self.assertIsNone(self._model_id(self.ix))

    def test_unknown_accession_is_skipped(self):
        out = self._run(_row("Q99999", "P04637", "AF-1", "0.9", "0.9"))
        self.assertIn("1 with an accession not in HIPPIE", out)

    def test_only_flag_requires_file(self):
        with self.assertRaises(CommandError):
            call_command("hippie_update", "--alphafold-only", stdout=StringIO())

    def test_missing_column_fails(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "af.tsv"
            path.write_text("uniprot_accession_A\tuniprot_accession_B\n")
            with self.assertRaises(CommandError):
                call_command(
                    "hippie_update",
                    "--alphafold",
                    str(path),
                    "--alphafold-only",
                    stdout=StringIO(),
                )


class AlphaFoldLinkRenderTest(HippieTestCase):
    LABEL = "Interaction in AlphaFold Protein Structure Database"

    def test_link_when_model_id_set(self):
        Interaction.objects.filter(pk=self.ix.pk).update(
            alphafold_model_id="AF-0000000210970416"
        )
        r = self.client.get(
            reverse("hippie_website:interaction_detail", args=[self.ix.pk])
        )
        self.assertContains(
            r, 'href="https://alphafold.ebi.ac.uk/entry/AF-0000000210970416"'
        )
        self.assertContains(r, self.LABEL)

    def test_not_available_when_unset(self):
        r = self.client.get(
            reverse("hippie_website:interaction_detail", args=[self.ix.pk])
        )
        self.assertIsNone(r.context["alphafold_url"])
        self.assertContains(r, self.LABEL)
        self.assertNotContains(r, "alphafold.ebi.ac.uk/entry/")

    def test_noninteraction_page_shows_not_available(self):
        ni = make_noninteraction(self.brca1, self.egfr)
        r = self.client.get(
            reverse("hippie_website:noninteraction_detail", args=[ni.pk])
        )
        self.assertIsNone(r.context["alphafold_url"])
        self.assertContains(r, self.LABEL)
