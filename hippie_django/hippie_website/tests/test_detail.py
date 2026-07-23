from django.urls import reverse

from ..models import (
    BaitPreyAssociation,
    NonInteraction,
    Publication,
    Source,
)
from ..views import (
    _resolve_noninteraction_pair,
)
from .factories import (
    HippieTestCase,
    make_protein,
    make_noninteraction,
)


class DetailViewContextTest(HippieTestCase):
    def test_interaction_detail_context_keys(self):
        r = self.client.get(
            reverse("hippie_website:interaction_detail", args=[self.ix.pk])
        )
        ctx = r.context
        for key in (
            "interaction",
            "p1",
            "p2",
            "sources",
            "publications",
            "experiments",
            "species",
        ):
            self.assertIn(key, ctx)

    def test_interaction_detail_p1_p2_structure(self):
        r = self.client.get(
            reverse("hippie_website:interaction_detail", args=[self.ix.pk])
        )
        ctx = r.context
        for side in ("p1", "p2"):
            d = ctx[side]
            for key in ("protein", "uniprot_id", "gene_id", "symbol"):
                self.assertIn(key, d)


# ---------------------------------------------------------------------------
# 11. NonInteraction model
# ---------------------------------------------------------------------------


class NonInteractionModelTest(HippieTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.ni = make_noninteraction(cls.brca1, cls.tp53, score=0.3)

    def test_str_contains_both_proteins_and_score(self):
        s = str(self.ni)
        self.assertIn("BRCA1", s)
        self.assertIn("TP53", s)
        self.assertIn("0.3", s)

    def test_canonical_order_after_make(self):
        self.assertLessEqual(self.ni.protein_1_id, self.ni.protein_2_id)

    def test_make_noninteraction_canonicalizes_reversed_input(self):
        a = make_protein("NI_TEST_A")
        b = make_protein("NI_TEST_B")
        ni = make_noninteraction(b, a)
        self.assertLessEqual(ni.protein_1_id, ni.protein_2_id)

    def test_score_stored_correctly(self):
        self.assertAlmostEqual(self.ni.score, 0.3)

    def test_constraint_names_present(self):
        names = {c.name for c in NonInteraction._meta.constraints}
        self.assertIn("noninteraction_canonical_order", names)
        self.assertIn("noninteraction_score_range", names)
        self.assertIn("noninteraction_unique_pair", names)


# ---------------------------------------------------------------------------
# 12. noninteraction_detail_view
# ---------------------------------------------------------------------------


class NoninteractionDetailViewTest(HippieTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.ni = make_noninteraction(cls.brca1, cls.tp53, score=0.25)
        pub = Publication.objects.create(pmid=99001)
        cls.bp_assoc = BaitPreyAssociation.objects.create(
            noninteraction=cls.ni,
            number_of_tests=1,
            number_of_observed=1,
        )
        cls.bp_assoc.publications.add(pub)

    def test_200(self):
        r = self.client.get(
            reverse("hippie_website:noninteraction_detail", args=[self.ni.pk])
        )
        self.assertEqual(r.status_code, 200)

    def test_404(self):
        r = self.client.get(
            reverse("hippie_website:noninteraction_detail", args=[99999])
        )
        self.assertEqual(r.status_code, 404)

    def test_context_keys(self):
        r = self.client.get(
            reverse("hippie_website:noninteraction_detail", args=[self.ni.pk])
        )
        for key in (
            "noninteraction",
            "p1",
            "p2",
            "bait_prey_total_tested",
            "bait_prey_times_observed",
            "pair_score",
            "pair_label",
            "is_noninteraction",
        ):
            self.assertIn(key, r.context)

    def test_is_noninteraction_flag(self):
        r = self.client.get(
            reverse("hippie_website:noninteraction_detail", args=[self.ni.pk])
        )
        self.assertTrue(r.context["is_noninteraction"])

    def test_p1_p2_structure(self):
        r = self.client.get(
            reverse("hippie_website:noninteraction_detail", args=[self.ni.pk])
        )
        for side in ("p1", "p2"):
            d = r.context[side]
            for key in ("protein", "uniprot_id", "gene_id", "symbol"):
                self.assertIn(key, d)

    def test_bait_prey_counts(self):
        r = self.client.get(
            reverse("hippie_website:noninteraction_detail", args=[self.ni.pk])
        )
        ctx = r.context
        self.assertEqual(ctx["bait_prey_total_tested"], 1)
        self.assertEqual(ctx["bait_prey_times_observed"], 1)

    def test_bait_prey_zero_when_no_tests(self):
        ni2 = make_noninteraction(self.brca1, self.egfr, score=0.1)
        r = self.client.get(
            reverse("hippie_website:noninteraction_detail", args=[ni2.pk])
        )
        ctx = r.context
        self.assertEqual(ctx["bait_prey_total_tested"], 0)
        self.assertEqual(ctx["bait_prey_times_observed"], 0)


# ---------------------------------------------------------------------------
# 13. _resolve_noninteraction_pair
# ---------------------------------------------------------------------------


class ResolveNoninteractionPairTest(HippieTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.ni = make_noninteraction(cls.brca1, cls.tp53, score=0.25)

    def test_known_pair_found(self):
        r = _resolve_noninteraction_pair("BRCA1", "TP53", 0)
        self.assertAlmostEqual(r["score"], 0.25, places=2)
        self.assertIsNotNone(r["interaction_id"])

    def test_reversed_pair_still_found(self):
        r = _resolve_noninteraction_pair("TP53", "BRCA1", 0)
        self.assertGreater(r["score"], 0)

    def test_unknown_protein_returns_minus_one(self):
        r = _resolve_noninteraction_pair("FAKEPROT", "TP53", 0)
        self.assertEqual(r["score"], -1.0)
        self.assertIsNone(r["interaction_id"])

    def test_proteins_exist_but_no_noninteraction(self):
        # BRCA1–EGFR: both exist, no NonInteraction
        r = _resolve_noninteraction_pair("BRCA1", "EGFR", 0)
        self.assertEqual(r["score"], -1.0)

    def test_is_noninteraction_true_when_found(self):
        r = _resolve_noninteraction_pair("BRCA1", "TP53", 0)
        self.assertTrue(r["is_noninteraction"])

    def test_is_noninteraction_true_when_not_found(self):
        r = _resolve_noninteraction_pair("FAKEPROT", "TP53", 0)
        self.assertTrue(r["is_noninteraction"])

    def test_source_and_experiment_count_are_none(self):
        r = _resolve_noninteraction_pair("BRCA1", "TP53", 0)
        self.assertIsNone(r["source_count"])
        self.assertIsNone(r["experiment_count"])

    def test_detail_url_uses_noninteraction_endpoint(self):
        r = _resolve_noninteraction_pair("BRCA1", "TP53", 0)
        self.assertIn("noninteraction", r["detail_url"])

    def test_input_order_preserved(self):
        r = _resolve_noninteraction_pair("BRCA1", "TP53", 7)
        self.assertEqual(r["input_order"], 7)


# ---------------------------------------------------------------------------
# 14. protein_query_api — show parameter
# ---------------------------------------------------------------------------


class BaitPreyModelTest(HippieTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        pub1 = Publication.objects.create(pmid=55555)
        pub2 = Publication.objects.create(pmid=55556)
        cls.ni = make_noninteraction(cls.brca1, cls.tp53, score=0.2)
        cls.bp_assoc = BaitPreyAssociation.objects.create(
            noninteraction=cls.ni,
            number_of_tests=2,
            number_of_observed=1,
        )
        cls.bp_assoc.publications.add(pub1, pub2)

    def test_bait_prey_assoc_str(self):
        s = str(self.bp_assoc)
        self.assertIsInstance(s, str)
        self.assertIn("number_of_tests", s)

    def test_bait_prey_assoc_linked_to_noninteraction(self):
        self.assertEqual(self.bp_assoc.noninteraction_id, self.ni.pk)
        self.assertIsNone(self.bp_assoc.interaction)

    def test_noninteraction_detail_counts_positive_tests_only(self):
        r = self.client.get(
            reverse("hippie_website:noninteraction_detail", args=[self.ni.pk])
        )
        ctx = r.context
        self.assertEqual(ctx["bait_prey_total_tested"], 2)
        self.assertEqual(ctx["bait_prey_times_observed"], 1)


# ---------------------------------------------------------------------------
# 22. _protein_display — isoform_uid parameter
# ---------------------------------------------------------------------------


class SourceLinkTest(HippieTestCase):
    def test_helpers_resolve_case_insensitively(self):
        from ..source_links import homepage_url, pair_search_url

        self.assertEqual(homepage_url("BioGRID"), "https://thebiogrid.org/")
        self.assertEqual(homepage_url("biogrid"), "https://thebiogrid.org/")
        self.assertEqual(homepage_url("pdb"), "https://www.rcsb.org/")  # alias
        self.assertEqual(homepage_url("no-such-db"), "")
        self.assertEqual(
            pair_search_url("IntAct", "P04637", "Q00987"),
            "https://www.ebi.ac.uk/intact/search?query=id:P04637%20AND%20id:Q00987%20AND%20source:intact",
        )
        # Scoped to the source DB (matches a confirmed-working IntAct URL).
        self.assertEqual(
            pair_search_url("DIP", "P42858", "Q13501"),
            "https://www.ebi.ac.uk/intact/search?query=id:P42858%20AND%20id:Q13501%20AND%20source:dip",
        )
        # Hyphenated source token is quoted (MIQL treats a bare '-' as NOT).
        self.assertTrue(
            pair_search_url("bhf-ucl", "A", "B").endswith(
                "%20AND%20source:%22bhf-ucl%22"
            )
        )
        self.assertIsNone(pair_search_url("BioGRID", "P04637", "Q00987"))
        self.assertIsNone(pair_search_url("IntAct", None, "Q00987"))

    def test_detail_view_annotates_pair_url(self):
        intact = Source.objects.create(
            name="IntAct", url="https://www.ebi.ac.uk/intact/"
        )
        self.ix.sources.add(intact)
        r = self.client.get(
            reverse("hippie_website:interaction_detail", args=[self.ix.pk])
        )
        by_name = {s.name: s for s in r.context["sources"]}
        url = by_name["IntAct"].pair_url
        self.assertTrue(url.startswith("https://www.ebi.ac.uk/intact/search?query="))
        self.assertIn("id:P38398", url)
        self.assertIn("id:P04637", url)
        self.assertIn("source:intact", url)
        # BioGRID has no key-free pairwise web URL → homepage-only.
        self.assertIsNone(by_name["BioGRID"].pair_url)

    def test_assign_source_urls_backfills_blank_only(self):
        from ..management.commands.hippie_update import _assign_source_urls

        blank = Source.objects.create(name="dip", url="")
        manual = Source.objects.create(name="mint", url="https://manual.example/")
        unknown = Source.objects.create(name="no-such-db", url="")

        _assign_source_urls()

        blank.refresh_from_db()
        manual.refresh_from_db()
        unknown.refresh_from_db()
        self.assertEqual(blank.url, "https://dip.doe-mbi.ucla.edu/")
        self.assertEqual(manual.url, "https://manual.example/")  # preserved
        self.assertEqual(unknown.url, "")  # unknown name stays blank
