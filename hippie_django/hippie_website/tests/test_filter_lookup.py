"""Tests for :mod:`hippie_website.filter_lookup`.

Covers each resolution tier and, more importantly, the failure behaviour: an
unresolved filter value must be reported, never silently dropped, because a
filter that collapses to "no filter" returns a wider result set than was asked
for and looks like a legitimate answer.
"""

from django.test import TestCase

from .. import filter_lookup as fl
from ..filter_categories import OTHER, experiment_category, source_category
from ..models import ExperimentType, InteractionType, Source, Tissue


class FilterLookupTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        # Two experiment types that share a category, so a category label has
        # something to expand to, plus one in a different category.
        cls.two_hybrid = ExperimentType.objects.create(
            name="two hybrid",
            psi_mi_code="MI:0018",
            quality_score=5.0,
            n_connected_interactions=10,
        )
        cls.two_hybrid_array = ExperimentType.objects.create(
            name="two hybrid array",
            psi_mi_code="MI:0397",
            quality_score=5.0,
            n_connected_interactions=4,
        )
        cls.pull_down = ExperimentType.objects.create(
            name="pull down",
            psi_mi_code="MI:0096",
            quality_score=4.0,
            n_connected_interactions=7,
        )
        cls.biogrid = Source.objects.create(name="BioGRID", n_connected_interactions=12)
        cls.intact = Source.objects.create(name="IntAct", n_connected_interactions=9)
        cls.direct = InteractionType.objects.create(
            name="direct interaction", n_connected_interactions=5
        )
        cls.liver = Tissue.objects.create(name="Liver", n_expressed_genes=3)

        # The category label the curated registry assigns to the two-hybrid
        # rows. Derived rather than hardcoded so a registry edit does not turn
        # this into a false failure.
        cls.two_hybrid_category = experiment_category("MI:0018", "two hybrid")

    # ── tier 1: ids ────────────────────────────────────────────────────────

    def test_integer_id_resolves(self):
        res = fl.resolve_filter_values("source", [self.biogrid.pk])
        self.assertTrue(res.ok)
        self.assertEqual(res.ids, [self.biogrid.pk])
        self.assertEqual(res.matched[0].matched_by, "id")

    def test_digit_string_resolves_as_id(self):
        res = fl.resolve_filter_values("source", [str(self.intact.pk)])
        self.assertEqual(res.ids, [self.intact.pk])
        self.assertEqual(res.matched[0].matched_by, "id")

    def test_unknown_id_is_unresolved(self):
        res = fl.resolve_filter_values("source", [999999])
        self.assertFalse(res.ok)
        self.assertEqual(res.ids, [])
        self.assertEqual(res.unresolved[0].reason, "unknown")

    # ── tier 2: names ──────────────────────────────────────────────────────

    def test_exact_name_resolves(self):
        res = fl.resolve_filter_values("source", ["BioGRID"])
        self.assertEqual(res.ids, [self.biogrid.pk])
        self.assertEqual(res.matched[0].matched_by, "name")

    def test_name_match_is_case_insensitive(self):
        res = fl.resolve_filter_values("source", ["biogrid"])
        self.assertEqual(res.ids, [self.biogrid.pk])
        self.assertEqual(res.matched[0].matched_by, "name")

    # ── tier 3: PSI-MI codes ───────────────────────────────────────────────

    def test_psi_mi_code_resolves(self):
        res = fl.resolve_filter_values("experiment", ["MI:0096"])
        self.assertEqual(res.ids, [self.pull_down.pk])
        self.assertEqual(res.matched[0].matched_by, "psi_mi_code")

    def test_psi_mi_code_is_case_insensitive(self):
        res = fl.resolve_filter_values("experiment", ["mi:0096"])
        self.assertEqual(res.ids, [self.pull_down.pk])

    # ── tier 4: category labels ────────────────────────────────────────────

    def test_category_label_expands_to_every_member(self):
        res = fl.resolve_filter_values("experiment", [self.two_hybrid_category])
        self.assertTrue(res.ok)
        self.assertEqual(res.matched[0].matched_by, "category")
        self.assertCountEqual(res.ids, [self.two_hybrid.pk, self.two_hybrid_array.pk])
        # And nothing from another category leaked in.
        self.assertNotIn(self.pull_down.pk, res.ids)

    def test_category_label_is_case_insensitive(self):
        res = fl.resolve_filter_values(
            "experiment", [self.two_hybrid_category.casefold()]
        )
        self.assertEqual(res.matched[0].matched_by, "category")

    def test_declared_category_with_no_rows_does_not_resolve(self):
        """A declared label with no live rows must fail, not expand to nothing.

        An empty expansion reaches the query as "no filter", which silently
        widens the result set instead of answering what was asked. So a category
        nothing currently belongs to is treated as unresolved — the same as a
        typo — and is not offered by ``list_filter_options`` either.
        """
        empty = self._empty_source_category()
        res = fl.resolve_filter_values("source", [empty])
        self.assertFalse(res.ok)
        self.assertEqual(res.ids, [])
        self.assertEqual(res.matched, [])
        self.assertEqual(res.unresolved[0].value, empty)

    def test_empty_category_is_not_offered_as_a_suggestion(self):
        """The did-you-mean pool lists populated categories only."""
        empty = self._empty_source_category()
        res = fl.resolve_filter_values("source", [empty[:6] + "zzz"])
        self.assertFalse(res.ok)
        self.assertNotIn(empty, res.unresolved[0].candidates)

    def test_populated_category_order_drops_empty_categories(self):
        populated = fl.populated_category_order_for("source")
        self.assertNotIn(self._empty_source_category(), populated)
        self.assertIn(source_category(self.biogrid.name), populated)
        # Still a subset of the declared order, in declared order.
        declared = fl.category_order_for("source")
        self.assertEqual(populated, [c for c in declared if c in populated])

    def _empty_source_category(self) -> str:
        """A declared source category that no test row belongs to."""
        return next(
            label
            for label in fl.category_order_for("source")
            if label
            not in {
                source_category(self.biogrid.name),
                source_category(self.intact.name),
            }
        )

    # ── tier 5: substrings ─────────────────────────────────────────────────

    def test_unique_substring_resolves(self):
        res = fl.resolve_filter_values("experiment", ["pull"])
        self.assertEqual(res.ids, [self.pull_down.pk])
        self.assertEqual(res.matched[0].matched_by, "substring")

    def test_substring_matching_only_one_row_is_not_ambiguous(self):
        """ "two hybrid a" is inside "two hybrid array" only, so it resolves."""
        res = fl.resolve_filter_values("experiment", ["two hybrid a"])
        self.assertTrue(res.ok)
        self.assertEqual(res.ids, [self.two_hybrid_array.pk])

    def test_ambiguous_substring_is_reported_with_candidates(self):
        res = fl.resolve_filter_values("experiment", ["hybrid"])
        self.assertFalse(res.ok)
        self.assertEqual(res.ids, [])
        self.assertEqual(res.unresolved[0].reason, "ambiguous")
        self.assertCountEqual(
            res.unresolved[0].candidates, ["two hybrid", "two hybrid array"]
        )

    # ── unresolved ─────────────────────────────────────────────────────────

    def test_unknown_value_offers_near_misses(self):
        res = fl.resolve_filter_values("experiment", ["two hybrud"])
        self.assertFalse(res.ok)
        self.assertEqual(res.unresolved[0].reason, "unknown")
        self.assertIn("two hybrid", res.unresolved[0].candidates)

    def test_partial_resolution_keeps_the_good_values(self):
        res = fl.resolve_filter_values("source", ["BioGRID", "nosuchsource"])
        self.assertFalse(res.ok)
        self.assertEqual(res.ids, [self.biogrid.pk])
        self.assertEqual(len(res.unresolved), 1)

    # ── general behaviour ──────────────────────────────────────────────────

    def test_empty_input_resolves_to_empty_without_error(self):
        for values in ([], None):
            res = fl.resolve_filter_values("source", values)
            self.assertTrue(res.ok)
            self.assertEqual(res.ids, [])
            self.assertEqual(res.echo(), {})

    def test_ids_are_deduplicated_and_order_preserving(self):
        res = fl.resolve_filter_values(
            "experiment",
            [self.two_hybrid_category, "two hybrid", str(self.pull_down.pk)],
        )
        self.assertEqual(len(res.ids), len(set(res.ids)))
        # The category expansion came first, so its members lead.
        self.assertEqual(res.ids[-1], self.pull_down.pk)

    def test_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            fl.resolve_filter_values("nonsense", ["x"])

    def test_filter_field_mapping(self):
        self.assertEqual(fl.filter_field_for("source"), "source_ids")
        self.assertEqual(fl.filter_field_for("experiment"), "experiment_ids")
        self.assertEqual(
            fl.filter_field_for("interaction_type"), "interaction_type_ids"
        )
        self.assertEqual(fl.filter_field_for("tissue"), "tissue_ids")

    def test_options_expose_psi_mi_code_for_experiments(self):
        codes = {o["name"]: o.get("psi_mi_code") for o in fl.options_for("experiment")}
        self.assertEqual(codes["pull down"], "MI:0096")

    def test_every_kind_is_resolvable(self):
        cases = {
            "source": "BioGRID",
            "experiment": "pull down",
            "interaction_type": "direct interaction",
            "tissue": "Liver",
        }
        for kind, value in cases.items():
            with self.subTest(kind=kind):
                res = fl.resolve_filter_values(kind, [value])
                self.assertTrue(res.ok, f"{kind}: {value!r} did not resolve")
                self.assertEqual(len(res.ids), 1)

    def test_category_order_pins_other_last(self):
        for kind in fl.KINDS:
            with self.subTest(kind=kind):
                self.assertEqual(fl.category_order_for(kind)[-1], OTHER)

    # ── echo ───────────────────────────────────────────────────────────────

    def test_echo_reports_category_expansion(self):
        echo = fl.resolve_filter_values("experiment", [self.two_hybrid_category]).echo()
        entry = echo["matched"][0]
        self.assertEqual(entry["input"], self.two_hybrid_category)
        self.assertEqual(entry["matched_by"], "category")
        self.assertEqual(entry["n_matched"], 2)

    def test_echo_reports_did_you_mean(self):
        echo = fl.resolve_filter_values("experiment", ["two hybrud"]).echo()
        self.assertIn("two hybrid", echo["unresolved"][0]["did_you_mean"])

    def test_resolve_all_skips_empty_kinds(self):
        out = fl.resolve_all({"source": ["BioGRID"], "experiment": [], "tissue": None})
        self.assertEqual(set(out), {"source"})
