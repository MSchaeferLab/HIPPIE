"""Filter option lists, category registry, and the "nothing ticked" contract.

Covers the pieces the ML-Splits UX rework depends on:

* every filter option carries a category, and the display order ships with it;
* options no interaction uses are hidden — but only once the denormalised
  counter has actually been populated, so a fresh database never shows an
  empty filter;
* the registry in ``filter_categories`` covers every vocabulary row it is given;
* an empty list and an omitted list mean the same thing to the backend. The
  frontend leans on that: with every box ticked by default, "all ticked" and
  "none ticked" both serialise to an omitted parameter, and both must return the
  full result set.
"""

from django.test import TestCase
from django.urls import reverse

from .. import filter_categories as fc
from ..management.commands.update_tissue_data import recompute_tissue_gene_counts
from ..models import ExperimentType, InteractionType, Source, Tissue
from ..views import _filter_option_lists
from .factories import HippieTestCase, make_interaction, make_protein


class FilterOptionListTest(HippieTestCase):
    """``_filter_option_lists`` shape: categories, counts, ordering, hiding."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.itype = InteractionType.objects.create(
            name="direct interaction", psi_mi_code="MI:0407"
        )
        cls.ix.interaction_types.add(cls.itype)
        # A second source that no interaction references.
        cls.unused = Source.objects.create(name="I2D", n_connected_interactions=0)

    def test_every_option_has_a_category(self):
        meta = _filter_option_lists()
        for key in ("sources", "experiments", "interaction_types"):
            self.assertTrue(meta[key], f"{key} should not be empty")
            for opt in meta[key]:
                self.assertIn("category", opt, f"{key} option missing category: {opt}")
                self.assertTrue(opt["category"])

    def test_category_order_shipped_for_each_vocabulary(self):
        order = _filter_option_lists()["category_order"]
        for key in ("sources", "experiments", "interaction_types"):
            self.assertTrue(order[key], f"no display order for {key}")
            self.assertIn(fc.OTHER, order[key], "Other must sort somewhere")

    def test_categories_come_from_the_registry(self):
        meta = _filter_option_lists()
        by_name = {o["name"]: o["category"] for o in meta["sources"]}
        self.assertEqual(by_name["BioGRID"], fc.INTERACTION_DB)
        exp = {o["name"]: o["category"] for o in meta["experiments"]}
        self.assertEqual(exp["Two-hybrid"], fc.TWO_HYBRID)  # MI:0018
        itypes = {o["name"]: o["category"] for o in meta["interaction_types"]}
        self.assertEqual(itypes["direct interaction"], fc.DIRECT)

    def test_zero_count_options_hidden_once_counts_exist(self):
        # BioGRID has n_connected_interactions=1 (see factories), I2D has 0.
        names = [o["name"] for o in _filter_option_lists()["sources"]]
        self.assertIn("BioGRID", names)
        self.assertNotIn("I2D", names)

    def test_counts_are_reported_when_known(self):
        src = next(
            o for o in _filter_option_lists()["sources"] if o["name"] == "BioGRID"
        )
        self.assertEqual(src["count"], 1)

    def test_nothing_hidden_while_the_counter_is_unpopulated(self):
        """A database that has never run ``hippie_update`` must still show options.

        The counter is a cache. If it reads 0 everywhere we cannot tell "unused"
        from "never computed", and hiding on that would empty the whole filter —
        a far worse failure than listing an option nobody uses.
        """
        Source.objects.update(n_connected_interactions=0)
        sources = _filter_option_lists()["sources"]
        names = [o["name"] for o in sources]
        self.assertIn("BioGRID", names)
        self.assertIn("I2D", names)
        # …and no misleading zeroes are sent along.
        self.assertNotIn("count", sources[0])


class TissueGroupingTest(TestCase):
    """Tissues group by body system, with the GTEx organ prefix as a sub-level."""

    @classmethod
    def setUpTestData(cls):
        from ..models import Gene, GeneTissue

        cls.names = [
            "Brain - Cortex",
            "Brain - Amygdala",
            "Nerve - Tibial",
            "Liver",
            "Liver - Hepatocyte",
            "Lung",
            "Some Future Organ - Region",  # deliberately uncategorised
        ]
        gene = Gene.objects.create(entrez_id=1, entrez_name="A1BG")
        for name in cls.names:
            tissue = Tissue.objects.create(name=name)
            GeneTissue.objects.create(gene=gene, tissue=tissue, median_rpkm=5.0)
        recompute_tissue_gene_counts()

    def _tissues(self):
        return _filter_option_lists()["tissues"]

    def test_body_system_categories_assigned(self):
        by_name = {o["name"]: o["category"] for o in self._tissues()}
        self.assertEqual(by_name["Brain - Cortex"], fc.NERVOUS)
        self.assertEqual(by_name["Nerve - Tibial"], fc.NERVOUS)
        self.assertEqual(by_name["Liver"], fc.DIGESTIVE)
        self.assertEqual(by_name["Lung"], fc.RESPIRATORY)

    def test_unknown_organ_falls_into_other(self):
        """A tissue GTEx adds later must still appear, not vanish."""
        by_name = {o["name"]: o["category"] for o in self._tissues()}
        self.assertEqual(by_name["Some Future Organ - Region"], fc.OTHER)

    def test_other_sorts_last_in_the_display_order(self):
        order = _filter_option_lists()["category_order"]["tissues"]
        self.assertEqual(order[-1], fc.OTHER)

    def test_subcategory_only_where_an_organ_has_several_tissues(self):
        by_name = {o["name"]: o for o in self._tissues()}
        # Brain and Liver have two entries each → sub-heading.
        self.assertEqual(by_name["Brain - Cortex"]["subcategory"], "Brain")
        self.assertEqual(by_name["Liver - Hepatocyte"]["subcategory"], "Liver")
        # A bare organ name still belongs to its own prefix group.
        self.assertEqual(by_name["Liver"]["subcategory"], "Liver")
        # Singletons sit directly under the body system.
        self.assertNotIn("subcategory", by_name["Nerve - Tibial"])
        self.assertNotIn("subcategory", by_name["Lung"])

    def test_tissues_carry_an_expressed_gene_count(self):
        self.assertTrue(all(o["count"] == 1 for o in self._tissues()))

    def test_tissue_without_expression_data_is_hidden(self):
        Tissue.objects.create(name="Placenta")  # no GeneTissue rows
        recompute_tissue_gene_counts()
        self.assertNotIn("Placenta", [o["name"] for o in self._tissues()])


class FilterCategoryRegistryTest(TestCase):
    """The registry must cover whatever vocabulary the database holds.

    Uncovered rows still render (they fall into "Other"), so this is a nudge to
    curate a newly imported term rather than a hard failure of the UI.
    """

    def test_experiment_codes_are_unique_across_categories(self):
        seen = set()
        for codes in fc._EXPERIMENT_CODES.values():
            for code in codes:
                self.assertNotIn(code, seen, f"{code} listed in two categories")
                seen.add(code)

    def test_interaction_type_names_are_unique_across_categories(self):
        seen = set()
        for names in fc._INTERACTION_TYPE_NAMES.values():
            for name in names:
                self.assertNotIn(name, seen, f"{name!r} listed in two categories")
                seen.add(name)

    def test_lookup_is_case_insensitive_and_falls_back(self):
        self.assertEqual(fc.source_category("IntAct"), fc.INTERACTION_DB)
        self.assertEqual(fc.source_category("intact"), fc.INTERACTION_DB)
        self.assertEqual(fc.interaction_type_category("ASSOCIATION"), fc.ASSOCIATION)
        self.assertEqual(fc.source_category("something new"), fc.OTHER)
        self.assertEqual(fc.experiment_category(""), fc.OTHER)
        self.assertEqual(fc.experiment_category("MI:9999"), fc.OTHER)

    def test_every_seeded_vocabulary_row_is_categorised(self):
        """Whatever is in the database maps to a bucket (possibly "Other")."""
        for src in Source.objects.all():
            self.assertTrue(fc.source_category(src.name))
        for exp in ExperimentType.objects.all():
            self.assertTrue(fc.experiment_category(exp.psi_mi_code, exp.name))
        for it in InteractionType.objects.all():
            self.assertTrue(fc.interaction_type_category(it.name))
        for t in Tissue.objects.all():
            self.assertTrue(fc.tissue_category(t.name))


class EmptyListMeansNoFilterTest(HippieTestCase):
    """Omitted, empty, and fully-selected lists must all return everything.

    This is the contract the "everything ticked by default" UI rests on: the
    frontend omits the parameter both when every box is ticked and when none is,
    so the two extremes have to agree with each other and with an unfiltered
    request. If this ever diverges, unticking the last box would silently empty
    the page.
    """

    def _count(self, **params):
        url = reverse("hippie_website:browse_interactions_api")
        resp = self.client.get(url, params)
        self.assertEqual(resp.status_code, 200)
        return resp.json()["total"]

    def test_omitted_and_empty_source_agree(self):
        baseline = self._count()
        self.assertEqual(self._count(source=""), baseline)

    def test_selecting_the_only_source_agrees_too(self):
        # "Everything ticked" is a real selection on the wire only if the
        # frontend fails to normalise it, so pin that it is harmless.
        self.assertEqual(self._count(source=self.src.pk), self._count())

    def test_a_proper_subset_does_narrow(self):
        """Guard against the filter being a no-op in all cases."""
        other = make_protein("MDM2", uniprot_name="MDM2_HUMAN", gene_id=4193)
        make_interaction(self.brca1, other, score=0.7)  # no source attached
        unused = Source.objects.create(name="DIP", n_connected_interactions=1)
        self.assertEqual(self._count(source=unused.pk), 0)
        self.assertGreater(self._count(), 0)


class PrecomputedSplitDownloadsTest(TestCase):
    """The Download page advertises the three pre-computed splits.

    The archives themselves are produced outside this repo and dropped into
    ``data/user_downloads/`` on the server, so this only pins the links.
    """

    FILES = (
        "HIPPIE-current.splits.high.tar.gz",
        "HIPPIE-current.splits.medium-high.tar.gz",
        "HIPPIE-current.splits.all.tar.gz",
    )

    def test_download_page_links_each_split(self):
        html = self.client.get(reverse("hippie_website:download")).content.decode()
        for name in self.FILES:
            self.assertIn(
                reverse("hippie_website:download_dataset", args=[name]),
                html,
                f"{name} is not linked from the Download page",
            )

    def test_ml_page_points_at_the_download_section(self):
        html = self.client.get(
            reverse("hippie_website:machine_learning")
        ).content.decode()
        self.assertIn(f"{reverse('hippie_website:download')}#ml-splits", html)
