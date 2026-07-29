"""Tests for the MCP tool functions in :mod:`hippie_mcp.server`.

The tools are called directly here — that is what the MCP layer does, minus the
transport — so these cover the tool contract: caps, truncation reporting,
filter-echo, and the distinction between "no evidence" and "no such protein".

Transport, schema generation, and ``structured_content`` are covered separately
in ``test_mcp_protocol``.
"""

from django.test import TestCase

from hippie_mcp import server
from hippie_mcp.shaping import DEFAULT_LIMIT, MAX_LIMIT

from ..filter_categories import experiment_category
from ..models import ExperimentType, Isoform, Source
from .factories import (
    make_interaction,
    make_noninteraction,
    make_protein,
    recompute_stats,
)


class McpToolTestCase(TestCase):
    """Fixture set with a hub protein, so truncation is exercised for real."""

    @classmethod
    def setUpTestData(cls):
        cls.tp53 = make_protein(
            "TP53", uniprot_name="P53_HUMAN", gene_id=7157, accession="P04637"
        )
        cls.mdm2 = make_protein(
            "MDM2", uniprot_name="MDM2_HUMAN", gene_id=4193, accession="Q00987"
        )
        cls.brca1 = make_protein(
            "BRCA1", uniprot_name="BRCA1_HUMAN", gene_id=672, accession="P38398"
        )
        # A protein with no interactions at all.
        cls.lonely = make_protein(
            "LONELY", uniprot_name="LONELY_HUMAN", gene_id=99991, accession="LON001"
        )

        cls.ix_mdm2 = make_interaction(cls.tp53, cls.mdm2, score=0.97)
        cls.ix_brca1 = make_interaction(cls.tp53, cls.brca1, score=0.51)
        # A recorded NON-interaction, which is a different answer from "absent".
        cls.non_ix = make_noninteraction(cls.mdm2, cls.brca1, score=0.2)

        cls.source = Source.objects.create(
            name="BioGRID", url="https://thebiogrid.org/", n_connected_interactions=2
        )
        cls.two_hybrid = ExperimentType.objects.create(
            name="two hybrid",
            psi_mi_code="MI:0018",
            quality_score=5.0,
            n_connected_interactions=1,
        )
        cls.pull_down = ExperimentType.objects.create(
            name="pull down",
            psi_mi_code="MI:0096",
            quality_score=4.0,
            n_connected_interactions=1,
        )
        cls.ix_mdm2.sources.add(cls.source)
        cls.ix_mdm2.experiments.add(cls.two_hybrid)
        cls.ix_brca1.experiments.add(cls.pull_down)

        # Enough partners to exceed the default limit.
        cls.hub = make_protein(
            "HUB", uniprot_name="HUB_HUMAN", gene_id=99992, accession="HUB001"
        )
        for i in range(DEFAULT_LIMIT + 7):
            partner = make_protein(
                f"P{i}",
                uniprot_name=f"P{i}_HUMAN",
                gene_id=90000 + i,
                accession=f"ACC{i:03d}",
            )
            make_interaction(cls.hub, partner, score=0.9 - i * 0.001)

        recompute_stats()


class ResolveProteinTests(McpToolTestCase):
    def test_resolves_by_accession(self):
        out = server.resolve_protein("P04637")
        self.assertTrue(out["found"])
        self.assertEqual(len(out["matches"]), 1)
        self.assertEqual(out["matches"][0]["symbol"], "TP53")
        self.assertEqual(out["matches"][0]["uniprot_id"], "P04637")

    def test_resolves_by_symbol_and_entrez_and_entry_name(self):
        for identifier in ("TP53", "7157", "P53_HUMAN"):
            with self.subTest(identifier=identifier):
                out = server.resolve_protein(identifier)
                self.assertTrue(out["found"])
                self.assertEqual(out["matches"][0]["uniprot_id"], "P04637")

    def test_reports_degree_and_avg_score(self):
        out = server.resolve_protein("P04637")
        match = out["matches"][0]
        self.assertEqual(match["degree"], 2)
        self.assertIsNotNone(match["avg_score"])

    def test_unknown_identifier_is_not_found(self):
        out = server.resolve_protein("NOT_A_PROTEIN")
        self.assertFalse(out["found"])
        self.assertEqual(out["matches"], [])
        self.assertIn("No HIPPIE protein", out["summary"])

    def test_summary_leads_the_payload(self):
        out = server.resolve_protein("P04637")
        self.assertEqual(next(iter(out)), "summary")


class GetInteractionsTests(McpToolTestCase):
    def test_returns_partners_ordered_by_score(self):
        out = server.get_interactions(proteins=["P04637"])
        self.assertEqual(out["total"], 2)
        self.assertFalse(out["truncated"])
        self.assertEqual([r["partner"] for r in out["rows"]], ["MDM2", "BRCA1"])

    def test_min_score_filters(self):
        out = server.get_interactions(proteins=["P04637"], min_score=0.9)
        self.assertEqual(out["total"], 1)
        self.assertEqual(out["rows"][0]["partner"], "MDM2")

    def test_caps_at_default_limit_and_says_so(self):
        out = server.get_interactions(proteins=["HUB001"])
        self.assertEqual(out["total"], DEFAULT_LIMIT + 7)
        self.assertEqual(out["returned"], DEFAULT_LIMIT)
        self.assertTrue(out["truncated"])
        # The count must be visible in the prose, not only in a field.
        self.assertIn(str(DEFAULT_LIMIT + 7), out["summary"])

    def test_limit_is_clamped_to_max(self):
        out = server.get_interactions(proteins=["HUB001"], limit=10_000)
        self.assertLessEqual(out["returned"], MAX_LIMIT)

    def test_no_partners_is_an_empty_not_an_error(self):
        out = server.get_interactions(proteins=["LON001"])
        self.assertEqual(out["total"], 0)
        self.assertEqual(out["rows"], [])
        self.assertNotIn("error", out)

    def test_unknown_protein_reports_no_proteins_found(self):
        out = server.get_interactions(proteins=["NOPE"])
        self.assertEqual(out["error"], "no_proteins_found")
        self.assertEqual(out["unresolved_identifiers"], ["NOPE"])

    def test_partially_unknown_input_still_answers(self):
        out = server.get_interactions(proteins=["P04637", "NOPE"])
        self.assertEqual(out["total"], 2)
        self.assertEqual(out["unresolved_identifiers"], ["NOPE"])

    def test_empty_input_is_rejected(self):
        self.assertEqual(server.get_interactions(proteins=[])["error"], "no_query")
        self.assertEqual(server.get_interactions(proteins=["  "])["error"], "no_query")

    def test_too_many_proteins_is_rejected(self):
        out = server.get_interactions(proteins=[f"X{i}" for i in range(60)])
        self.assertEqual(out["error"], "too_many_proteins")

    def test_summary_names_the_accession_it_actually_queried(self):
        out = server.get_interactions(proteins=["TP53"])
        self.assertIn("P04637", out["summary"])

    # ── filters ────────────────────────────────────────────────────────────

    def test_experiment_filter_by_name(self):
        out = server.get_interactions(proteins=["P04637"], experiments=["two hybrid"])
        self.assertEqual(out["total"], 1)
        self.assertEqual(out["rows"][0]["partner"], "MDM2")

    def test_experiment_filter_by_psi_mi_code(self):
        out = server.get_interactions(proteins=["P04637"], experiments=["MI:0018"])
        self.assertEqual(out["total"], 1)

    def test_experiment_filter_by_category_label(self):
        label = experiment_category("MI:0018", "two hybrid")
        out = server.get_interactions(proteins=["P04637"], experiments=[label])
        self.assertEqual(out["total"], 1)
        echo = out["resolved_filters"]["experiment"]["matched"][0]
        self.assertEqual(echo["matched_by"], "category")

    def test_source_filter_by_name(self):
        out = server.get_interactions(proteins=["P04637"], sources=["BioGRID"])
        self.assertEqual(out["total"], 1)

    def test_resolved_filters_are_echoed(self):
        out = server.get_interactions(proteins=["P04637"], sources=["BioGRID"])
        self.assertIn("source", out["resolved_filters"])

    def test_unresolvable_filter_refuses_to_run_the_query(self):
        """The important one: a bad filter must not silently widen the result."""
        out = server.get_interactions(
            proteins=["P04637"], experiments=["definitely-not-a-method"]
        )
        self.assertEqual(out["error"], "unresolved_filter")
        self.assertNotIn("rows", out)
        self.assertTrue(out["problems"])

    def test_noninteractions_can_be_requested(self):
        out = server.get_interactions(proteins=["Q00987"], show="noninteractions")
        self.assertEqual(out["total"], 1)
        self.assertTrue(out["rows"][0]["is_noninteraction"])


class CheckPairsTests(McpToolTestCase):
    def test_interacting_pair(self):
        out = server.check_pairs(pairs=[["P04637", "Q00987"]])
        row = out["rows"][0]
        self.assertEqual(row["outcome"], "interacts")
        self.assertEqual(row["score"], 0.97)
        self.assertEqual(row["interaction_id"], self.ix_mdm2.pk)

    def test_recorded_non_interaction_is_distinct_from_absence(self):
        out = server.check_pairs(pairs=[["Q00987", "P38398"]])
        self.assertEqual(out["rows"][0]["outcome"], "does_not_interact")

    def test_no_record_between_known_proteins(self):
        out = server.check_pairs(pairs=[["P04637", "LON001"]])
        self.assertEqual(out["rows"][0]["outcome"], "no_record")

    def test_unknown_identifier_is_its_own_outcome(self):
        out = server.check_pairs(pairs=[["P04637", "NOPE"]])
        self.assertEqual(out["rows"][0]["outcome"], "unknown_identifier")

    def test_counts_summarise_the_batch(self):
        out = server.check_pairs(
            pairs=[
                ["P04637", "Q00987"],
                ["P04637", "LON001"],
                ["P04637", "NOPE"],
            ]
        )
        self.assertEqual(out["counts"]["interacts"], 1)
        self.assertEqual(out["counts"]["no_record"], 1)
        self.assertEqual(out["counts"]["unknown_identifier"], 1)

    def test_one_row_per_input_pair(self):
        pairs = [["P04637", "Q00987"], ["P04637", "P38398"]]
        out = server.check_pairs(pairs=pairs)
        self.assertEqual(len(out["rows"]), len(pairs))

    def test_malformed_pairs_are_reported_not_crashed(self):
        out = server.check_pairs(pairs=[["P04637"], ["P04637", "Q00987"], []])
        self.assertEqual(len(out["rows"]), 1)
        self.assertEqual(out["malformed_pair_indexes"], [0, 2])

    def test_no_usable_pairs_is_rejected(self):
        self.assertEqual(server.check_pairs(pairs=[])["error"], "no_pairs")
        self.assertEqual(server.check_pairs(pairs=[["", ""]])["error"], "no_pairs")

    def test_too_many_pairs_is_rejected(self):
        out = server.check_pairs(pairs=[["A", "B"]] * 201)
        self.assertEqual(out["error"], "too_many_pairs")

    def test_unresolvable_filter_refuses_to_run(self):
        out = server.check_pairs(pairs=[["P04637", "Q00987"]], sources=["not-a-source"])
        self.assertEqual(out["error"], "unresolved_filter")


class GetInteractionDetailTests(McpToolTestCase):
    def test_returns_evidence(self):
        out = server.get_interaction_detail(interaction_id=self.ix_mdm2.pk)
        self.assertEqual(out["interaction_id"], self.ix_mdm2.pk)
        self.assertEqual(out["score"], 0.97)
        self.assertEqual({s["name"] for s in out["sources"]}, {"BioGRID"})
        self.assertEqual({e["psi_mi_code"] for e in out["experiments"]}, {"MI:0018"})
        self.assertEqual(
            {out["protein_a"]["symbol"], out["protein_b"]["symbol"]},
            {"TP53", "MDM2"},
        )

    def test_summary_states_the_evidence_volume(self):
        out = server.get_interaction_detail(interaction_id=self.ix_mdm2.pk)
        self.assertIn("score 0.97", out["summary"])

    def test_missing_interaction_is_reported_not_raised(self):
        out = server.get_interaction_detail(interaction_id=99_999_999)
        self.assertEqual(out["error"], "not_found")

    def test_payload_is_json_serialisable(self):
        import json

        out = server.get_interaction_detail(interaction_id=self.ix_mdm2.pk)
        json.dumps(out)  # must not raise


class ListFilterOptionsTests(McpToolTestCase):
    def test_overview_lists_every_vocabulary_without_dumping_options(self):
        out = server.list_filter_options()
        self.assertEqual(
            set(out["vocabularies"]),
            {"source", "experiment", "interaction_type", "tissue"},
        )
        entry = out["vocabularies"]["experiment"]
        self.assertEqual(entry["n_options"], 2)
        # The overview is the cheap call: counts and category labels only, never
        # the individual options (there are ~600 of them in production).
        self.assertEqual(set(entry), {"n_options", "categories"})
        self.assertNotIn("categories", out)

    def test_kind_returns_grouped_options(self):
        out = server.list_filter_options(kind="experiment")
        names = [
            entry["name"] for options in out["categories"].values() for entry in options
        ]
        self.assertCountEqual(names, ["pull down", "two hybrid"])

    def test_query_filters_options(self):
        out = server.list_filter_options(kind="experiment", query="pull")
        self.assertEqual(out["n_options"], 1)

    def test_experiment_options_carry_psi_mi_codes(self):
        out = server.list_filter_options(kind="experiment")
        codes = {
            entry["name"]: entry.get("psi_mi_code")
            for options in out["categories"].values()
            for entry in options
        }
        self.assertEqual(codes["two hybrid"], "MI:0018")


class IsoformToolTests(TestCase):
    """Isoform expansion is a 3-way mode on every query, so it needs its own set."""

    @classmethod
    def setUpTestData(cls):
        cls.canonical = make_protein(
            "SRC", uniprot_name="SRC_HUMAN", gene_id=6714, accession="P12931"
        )
        cls.partner = make_protein(
            "PTK2", uniprot_name="PTK2_HUMAN", gene_id=5747, accession="Q05397"
        )
        cls.isoform = Isoform.objects.create(
            gene=cls.canonical.gene,
            uniprot_accession="P12931-2",
            uniprot_name="SRC_HUMAN_ISO2",
            general_protein=cls.canonical,
            is_reviewed=True,
        )
        make_interaction(cls.canonical, cls.partner, score=0.8)
        make_interaction(cls.isoform, cls.partner, score=0.6)
        recompute_stats()

    def test_general_mode_excludes_isoform_edges(self):
        out = server.get_interactions(proteins=["P12931"], isoform_mode="general")
        self.assertEqual(out["total"], 1)

    def test_both_mode_includes_isoform_edges(self):
        out = server.get_interactions(proteins=["P12931"], isoform_mode="both")
        self.assertGreater(out["total"], 1)

    def test_isoform_accession_resolves(self):
        out = server.resolve_protein("P12931-2")
        self.assertTrue(out["found"])
        self.assertTrue(out["matches"][0]["is_isoform"])
