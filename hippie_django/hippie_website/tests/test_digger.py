"""Tests for the DIGGER cross-links feature.

Covers the pure URL builders (``digger_links.py``), the local idmapping parser +
``_populate_ensembl_ids`` (Phase 1, ``skip_api=True``), and end-to-end rendering
of the "Further information" card on both detail pages.
"""

from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from django.core import signing
from django.urls import reverse

from ..digger_links import interaction_digger, protein_digger_url
from ..models import Isoform
from .factories import HippieTestCase, make_interaction


# ---------------------------------------------------------------------------
# Pure URL builders (no DB)
# ---------------------------------------------------------------------------


class DiggerLinksTest(HippieTestCase):
    def test_canonical_uses_gene_ensg(self):
        url = protein_digger_url(
            is_isoform=False, ensg=["ENSG00000141510"], enst=[], ensp=[]
        )
        self.assertEqual(
            url, "https://exbio.wzw.tum.de/digger/ID/gene/human/ENSG00000141510"
        )

    def test_canonical_empty_ensg_is_none(self):
        self.assertIsNone(
            protein_digger_url(is_isoform=False, ensg=[], enst=[], ensp=[])
        )

    def test_isoform_prefers_enst(self):
        url = protein_digger_url(
            is_isoform=True,
            ensg=["ENSG00000135679"],
            enst=["ENST00000258149"],
            ensp=["ENSP00000258149"],
        )
        self.assertEqual(
            url, "https://exbio.wzw.tum.de/digger/ID/human/ENST00000258149"
        )

    def test_isoform_falls_back_to_ensp(self):
        url = protein_digger_url(
            is_isoform=True, ensg=["ENSG1"], enst=[], ensp=["ENSP00000258149"]
        )
        self.assertEqual(
            url, "https://exbio.wzw.tum.de/digger/ID/human/ENSP00000258149"
        )

    def test_isoform_falls_back_to_gene_ensg(self):
        url = protein_digger_url(
            is_isoform=True, ensg=["ENSG00000135679"], enst=[], ensp=[]
        )
        self.assertEqual(
            url, "https://exbio.wzw.tum.de/digger/ID/gene/human/ENSG00000135679"
        )

    def test_isoform_all_empty_is_none(self):
        self.assertIsNone(
            protein_digger_url(is_isoform=True, ensg=[], enst=[], ensp=[])
        )

    def test_first_non_empty_entry_is_used(self):
        url = protein_digger_url(
            is_isoform=True, ensg=[], enst=["", "ENST00000000002"], ensp=[]
        )
        self.assertTrue(url.endswith("/ID/human/ENST00000000002"))

    def test_interaction_both_isoforms_uses_accessions(self):
        res = interaction_digger(
            p1_is_isoform=True,
            p2_is_isoform=True,
            p1_enst_p="ENST00000258149",
            p2_enst_p="ENST00000607003",
            g1_ensg=[],
            g2_ensg=[],
            handoff_secret="test-secret",
        )
        self.assertEqual(res["kind"], "link")
        self.assertEqual(
            res["url"],
            "https://exbio.wzw.tum.de/digger/ID/gene/human/multiple/ENST00000258149,ENST00000607003",
        )

    def test_interaction_both_canonical_builds_token_link(self):
        res = interaction_digger(
            p1_is_isoform=False,
            p2_is_isoform=False,
            p1_enst_p="",
            p2_enst_p="",
            g1_ensg=["ENSG00000012048"],
            g2_ensg=["ENSG00000141510"],
            handoff_secret="test-secret",
        )
        self.assertEqual(res["kind"], "link")
        self.assertTrue(
            res["url"].startswith(
                "https://exbio.wzw.tum.de/digger/receive-token/?token="
            )
        )
        token = res["url"].split("token=", 1)[1]
        self.assertEqual(
            signing.loads(token, key="test-secret", salt="hippie-handoff"),
            {"organism": "human", "input": ["ENSG00000012048", "ENSG00000141510"]},
        )

    def test_interaction_canonical_missing_ensg_is_none(self):
        res = interaction_digger(
            p1_is_isoform=False,
            p2_is_isoform=False,
            p1_enst_p="",
            p2_enst_p="",
            g1_ensg=["ENSG00000012048"],
            g2_ensg=[],
            handoff_secret="test-secret",
        )
        self.assertEqual(res["kind"], "none")

    def test_interaction_mixed_is_none(self):
        res = interaction_digger(
            p1_is_isoform=True,
            p2_is_isoform=False,
            p1_enst_p="ENST00000258149",
            p2_enst_p="",
            g1_ensg=[],
            g2_ensg=["ENSG00000141510"],
            handoff_secret="test-secret",
        )
        self.assertEqual(res["kind"], "none")


# ---------------------------------------------------------------------------
# Local idmapping parse + populate (Phase 1, skip_api)
# ---------------------------------------------------------------------------

_IDMAPPING_FIXTURE = "\n".join(
    [
        "P38398\tEnsembl\tENSG00000012048.1",
        "P38398\tEnsembl\tENSG00000012048.1",  # duplicate -> de-duped
        "P04637\tEnsembl\tENSG00000141510.19",
        "Q00987-11\tEnsembl_TRS\tENST00000258149.5",
        "Q00987-11\tEnsembl_TRS\tENST00000258149.5",  # duplicate
        "Q00987-11\tEnsembl_PRO\tENSP00000258149.3",
        "Q00987\tEnsembl_TRS\tENST99999999999.1",  # canonical TRS -> ignored
    ]
)


class DiggerLocalPopulateTest(HippieTestCase):
    def _run_populate(self):
        from ..management.commands import hippie_update

        with TemporaryDirectory() as d:
            path = Path(d) / "idmapping.dat"
            path.write_text(_IDMAPPING_FIXTURE + "\n")
            with mock.patch.object(hippie_update, "data_path", return_value=path):
                hippie_update._populate_ensembl_ids(StringIO(), skip_api=True)

    def test_parser_strips_version_and_dedupes(self):
        from ..management.commands import hippie_update

        with TemporaryDirectory() as d:
            path = Path(d) / "idmapping.dat"
            path.write_text(_IDMAPPING_FIXTURE + "\n")
            with mock.patch.object(hippie_update, "data_path", return_value=path):
                acc_ensg, iso_enst, iso_ensp = hippie_update._parse_idmapping_ensembl()
        self.assertEqual(acc_ensg["P38398"], ["ENSG00000012048", "ENSG00000012048"])
        self.assertEqual(iso_enst["Q00987-11"], ["ENST00000258149", "ENST00000258149"])
        self.assertEqual(iso_ensp["Q00987-11"], ["ENSP00000258149"])
        # Canonical (no-dash) Ensembl_TRS rows are not collected as isoform ENSTs.
        self.assertNotIn("Q00987", iso_enst)

    def test_gene_ensg_populated_from_canonical_protein(self):
        self._run_populate()
        self.brca1.gene.refresh_from_db()  # P38398 / entrez 672
        self.tp53.gene.refresh_from_db()  # P04637 / entrez 7157
        self.assertEqual(self.brca1.gene.ensg, ["ENSG00000012048"])
        self.assertEqual(self.tp53.gene.ensg, ["ENSG00000141510"])

    def test_isoform_enst_ensp_populated(self):
        iso = Isoform.objects.create(
            uniprot_accession="Q00987-11",
            general_protein=self.brca1,
            gene=self.brca1.gene,
            uniprot_name="MDM2_11",
        )
        self._run_populate()
        iso.refresh_from_db()
        self.assertEqual(iso.enst, ["ENST00000258149"])
        self.assertEqual(iso.ensp, ["ENSP00000258149"])

    def test_unmapped_gene_gets_empty_list(self):
        self._run_populate()
        self.egfr.gene.refresh_from_db()  # P00533 not in the fixture
        self.assertEqual(self.egfr.gene.ensg, [])


# ---------------------------------------------------------------------------
# Card rendering on the detail pages
# ---------------------------------------------------------------------------


class DiggerCardRenderTest(HippieTestCase):
    def _set_gene_ensg(self, protein, ensg):
        protein.gene.ensg = ensg
        protein.gene.save(update_fields=["ensg"])

    def test_card_present_on_interaction_page(self):
        r = self.client.get(
            reverse("hippie_website:interaction_detail", args=[self.ix.pk])
        )
        self.assertContains(r, "Further information")
        self.assertIn("digger", r.context)

    def test_canonical_pair_renders_gene_links_and_token_link(self):
        self._set_gene_ensg(self.brca1, ["ENSG00000012048"])
        self._set_gene_ensg(self.tp53, ["ENSG00000141510"])
        r = self.client.get(
            reverse("hippie_website:interaction_detail", args=[self.ix.pk])
        )
        body = r.content.decode()
        self.assertIn("/digger/ID/gene/human/ENSG00000012048", body)
        self.assertIn("/digger/ID/gene/human/ENSG00000141510", body)
        # Both-canonical interaction link is a signed-token GET handoff.
        self.assertIn("/digger/receive-token/?token=", body)

    def test_mixed_pair_interaction_not_available(self):
        # brca1 canonical (with ENSG) × isoform of tp53.
        iso = Isoform.objects.create(
            uniprot_accession="P04637-2",
            general_protein=self.tp53,
            gene=self.tp53.gene,
            uniprot_name="P53_2",
            enst=["ENST00000269305"],
        )
        self._set_gene_ensg(self.brca1, ["ENSG00000012048"])
        ix = make_interaction(self.brca1, iso, score=0.5)
        r = self.client.get(reverse("hippie_website:interaction_detail", args=[ix.pk]))
        self.assertEqual(r.context["digger"]["interaction"]["kind"], "none")

    def test_isoform_pair_uses_multiple_endpoint(self):
        gene = self.brca1.gene
        iso_a = Isoform.objects.create(
            uniprot_accession="Q00987-11",
            general_protein=self.brca1,
            gene=gene,
            uniprot_name="A_11",
            enst=["ENST00000258149"],
        )
        iso_b = Isoform.objects.create(
            uniprot_accession="P37163-2",
            general_protein=self.tp53,
            gene=self.tp53.gene,
            uniprot_name="B_2",
            enst=["ENST00000000002"],
        )
        ix = make_interaction(iso_a, iso_b, score=0.6)
        r = self.client.get(reverse("hippie_website:interaction_detail", args=[ix.pk]))
        self.assertContains(
            r,
            "/digger/ID/gene/human/multiple/ENST00000258149,ENST00000000002",
        )
        self.assertContains(r, "/digger/ID/human/ENST00000258149")

    def test_noninteraction_page_shows_card(self):
        from .factories import make_noninteraction

        self._set_gene_ensg(self.brca1, ["ENSG00000012048"])
        self._set_gene_ensg(self.egfr, ["ENSG00000146648"])
        ni = make_noninteraction(self.brca1, self.egfr, score=0.2)
        r = self.client.get(
            reverse("hippie_website:noninteraction_detail", args=[ni.pk])
        )
        self.assertContains(r, "Further information")
        self.assertContains(r, "/digger/ID/gene/human/ENSG00000012048")
