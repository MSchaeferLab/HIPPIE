import json

from django.test import TestCase
from django.urls import reverse

from ..models import (
    Interaction,
    Isoform,
    NonInteraction,
    Protein,
    Source,
    ExperimentType,
    Tissue,
    GeneTissue,
)
from ..views import (
    _protein_display,
    _resolve_interaction_pair,
    _protein_ids_from_raw,
    _get_isoforms,
    _safe_int,
    _safe_float,
)
from ..query_filters import isoform_only_q, parse_isoform_mode
from .factories import (
    HippieTestCase,
    make_protein,
    make_interaction,
    make_noninteraction,
    recompute_flags,
)


class UrlSmokeTest(HippieTestCase):
    def test_index_get(self):
        r = self.client.get(reverse("hippie_website:index"))
        self.assertEqual(r.status_code, 200)

    def test_interaction_query_get(self):
        r = self.client.get(reverse("hippie_website:interaction_query"))
        self.assertEqual(r.status_code, 200)

    def test_network_query_get(self):
        r = self.client.get(reverse("hippie_website:network_query"))
        self.assertEqual(r.status_code, 200)

    def test_browse_get(self):
        r = self.client.get(reverse("hippie_website:browse"))
        self.assertEqual(r.status_code, 200)

    def test_download_get(self):
        r = self.client.get(reverse("hippie_website:download"))
        self.assertEqual(r.status_code, 200)

    def test_information_get(self):
        r = self.client.get(reverse("hippie_website:information"))
        self.assertEqual(r.status_code, 200)

    def test_interaction_detail_get(self):
        r = self.client.get(
            reverse("hippie_website:interaction_detail", args=[self.ix.pk])
        )
        self.assertEqual(r.status_code, 200)

    def test_interaction_detail_404(self):
        r = self.client.get(reverse("hippie_website:interaction_detail", args=[99999]))
        self.assertEqual(r.status_code, 404)

    def test_browse_api_get(self):
        r = self.client.get(reverse("hippie_website:browse_api"))
        self.assertEqual(r.status_code, 200)

    def test_browse_filter_meta_get(self):
        r = self.client.get(reverse("hippie_website:browse_filter_meta"))
        self.assertEqual(r.status_code, 200)


# ---------------------------------------------------------------------------
# 2. protein_query_api
# ---------------------------------------------------------------------------


class ProteinQueryApiTest(HippieTestCase):
    def _get(self, q):
        r = self.client.get(reverse("hippie_website:protein_query_api"), {"q": q})
        self.assertEqual(r.status_code, 200)
        return json.loads(r.content)

    def test_resolve_by_gene_symbol(self):
        data = self._get("BRCA1")
        self.assertIsNone(data["error"])
        self.assertEqual(data["query_protein"]["symbol"], "BRCA1")

    def test_resolve_by_uniprot_id(self):
        data = self._get("BRCA1_HUMAN")
        self.assertIsNone(data["error"])
        self.assertEqual(data["query_protein"]["name"], "BRCA1")

    def test_resolve_by_accession(self):
        data = self._get("P38398")
        self.assertIsNone(data["error"])
        self.assertEqual(data["query_protein"]["name"], "BRCA1")

    def test_resolve_isoform_query_exposes_isoform_uid(self):
        Isoform.objects.create(
            gene=self.brca1.gene,
            uniprot_name="BRCA1_2_HUMAN",
            uniprot_accession="P38398-2",
            general_protein=self.brca1,
        )
        data = self._get("P38398-2")
        self.assertIsNone(data["error"])
        self.assertEqual(data["query_protein"]["isoform_uniprot_id"], "P38398-2")

    def test_resolve_by_entrez_id(self):
        data = self._get("672")
        self.assertIsNone(data["error"])
        self.assertEqual(data["query_protein"]["name"], "BRCA1")

    def test_unknown_identifier_returns_error(self):
        data = self._get("DOESNOTEXIST")
        self.assertIsNotNone(data["error"])
        self.assertEqual(data["interactions"], [])

    def test_empty_query_returns_error(self):
        data = self._get("")
        self.assertIsNotNone(data["error"])

    def test_interaction_structure(self):
        """Jedes Interaction-Objekt hat alle erwarteten Felder."""
        data = self._get("BRCA1")
        self.assertGreater(len(data["interactions"]), 0)
        ix = data["interactions"][0]
        for key in (
            "id",
            "partner",
            "score",
            "source_count",
            "experiment_count",
            "detail_url",
        ):
            self.assertIn(key, ix)
        partner = ix["partner"]
        for key in ("id", "name", "symbol", "uniprot_id", "gene_id"):
            self.assertIn(key, partner)

    def test_score_range(self):
        """Score muss zwischen 0 und 1 liegen."""
        data = self._get("BRCA1")
        for ix in data["interactions"]:
            self.assertGreaterEqual(ix["score"], 0.0)
            self.assertLessEqual(ix["score"], 1.0)

    def test_partner_is_not_query_protein(self):
        """Partner darf nicht das gesuchte Protein selbst sein."""
        data = self._get("BRCA1")
        query_id = data["query_protein"]["id"]
        for ix in data["interactions"]:
            self.assertNotEqual(ix["partner"]["id"], query_id)


# ---------------------------------------------------------------------------
# 3. interaction_query_api
# ---------------------------------------------------------------------------


class InteractionQueryApiTest(HippieTestCase):
    def _post(self, pairs, status=200):
        r = self.client.post(
            reverse("hippie_website:interaction_query_api"),
            data=json.dumps({"pairs": pairs}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, status)
        return json.loads(r.content)

    def test_known_pair_found(self):
        pairs = [{"a": "BRCA1", "b": "TP53", "input_order": 0}]
        data = self._post(pairs)
        self.assertEqual(len(data["results"]), 1)
        result = data["results"][0]
        self.assertGreater(result["score"], 0)
        self.assertIsNotNone(result["interaction_id"])

    def test_reversed_pair_still_found(self):
        """Canonical ordering: auch wenn Reihenfolge vertauscht ist."""
        pairs = [{"a": "TP53", "b": "BRCA1", "input_order": 0}]
        data = self._post(pairs)
        self.assertGreater(data["results"][0]["score"], 0)

    def test_unknown_protein_returns_minus_one(self):
        pairs = [{"a": "FAKEPROT", "b": "TP53", "input_order": 0}]
        data = self._post(pairs)
        self.assertEqual(data["results"][0]["score"], -1.0)
        self.assertIsNone(data["results"][0]["interaction_id"])

    def test_no_interaction_returns_minus_one(self):
        """BRCA1–EGFR: Beide Proteine existieren, aber keine Interaction."""
        pairs = [{"a": "BRCA1", "b": "EGFR", "input_order": 0}]
        data = self._post(pairs)
        self.assertEqual(data["results"][0]["score"], -1.0)

    def test_batch_too_large_returns_400(self):
        pairs = [{"a": "BRCA1", "b": "TP53", "input_order": i} for i in range(201)]
        self._post(pairs, status=400)

    def test_invalid_json_returns_400(self):
        r = self.client.post(
            reverse("hippie_website:interaction_query_api"),
            data="not json",
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400)

    def test_multiple_pairs_preserves_order(self):
        pairs = [
            {"a": "BRCA1", "b": "TP53", "input_order": 0},
            {"a": "FAKEPROT", "b": "TP53", "input_order": 1},
        ]
        data = self._post(pairs)
        self.assertEqual(len(data["results"]), 2)
        orders = [r["input_order"] for r in data["results"]]
        self.assertEqual(orders, [0, 1])

    def test_result_contains_symbol_fields(self):
        pairs = [{"a": "BRCA1", "b": "TP53", "input_order": 0}]
        data = self._post(pairs)
        r = data["results"][0]
        for key in (
            "symbol_a",
            "symbol_b",
            "uniprot_a",
            "uniprot_b",
            "score",
            "source_count",
            "experiment_count",
            "input_order",
        ):
            self.assertIn(key, r)


# ---------------------------------------------------------------------------
# 4. browse_api
# ---------------------------------------------------------------------------


class BrowseApiTest(HippieTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.tissue = Tissue.objects.create(name="Brain")
        GeneTissue.objects.create(
            gene=cls.brca1.gene, tissue=cls.tissue, median_rpkm=1.0
        )

    def _get(self, **params):
        r = self.client.get(reverse("hippie_website:browse_api"), params)
        self.assertEqual(r.status_code, 200)
        return json.loads(r.content)

    def test_returns_total_and_proteins(self):
        data = self._get()
        self.assertIn("total", data)
        self.assertIn("proteins", data)
        self.assertIsInstance(data["proteins"], list)

    def test_total_matches_protein_count(self):
        data = self._get()
        self.assertEqual(data["total"], Protein.objects.count())

    def test_protein_entry_structure(self):
        data = self._get()
        self.assertGreater(len(data["proteins"]), 0)
        p = data["proteins"][0]
        for key in ("id", "symbol", "uniprot_id", "entrez_id", "degree", "avg_score"):
            self.assertIn(key, p)

    def test_tissue_filter_reduces_results(self):
        all_data = self._get()
        filt_data = self._get(tissue=self.tissue.pk)
        self.assertLess(filt_data["total"], all_data["total"])
        self.assertEqual(filt_data["total"], 1)

    def test_min_rpkm_filter(self):
        # brca1 has median_rpkm=1.0; threshold below → included
        data_low = self._get(tissue=self.tissue.pk, min_rpkm=0.5)
        self.assertEqual(data_low["total"], 1)
        # threshold above → excluded
        data_high = self._get(tissue=self.tissue.pk, min_rpkm=2.0)
        self.assertEqual(data_high["total"], 0)

    def test_source_filter(self):
        data = self._get(source=self.src.pk)
        # BRCA1 und TP53 haben eine Interaction mit BioGRID als Source
        self.assertGreater(data["total"], 0)

    def test_min_degree_filter(self):
        data = self._get(min_degree=1)
        # BRCA1 + TP53 have one interaction each; EGFR has none → excluded.
        self.assertEqual(data["total"], 2)
        for p in data["proteins"]:
            self.assertGreaterEqual(p["degree"], 1)

    def test_offset_and_limit(self):
        page1 = self._get(offset=0, limit=1)
        page2 = self._get(offset=1, limit=1)
        self.assertEqual(len(page1["proteins"]), 1)
        self.assertEqual(len(page2["proteins"]), 1)
        self.assertNotEqual(page1["proteins"][0]["id"], page2["proteins"][0]["id"])

    def test_total_unchanged_with_pagination(self):
        all_data = self._get()
        page = self._get(offset=0, limit=1)
        self.assertEqual(page["total"], all_data["total"])

    def test_protein_entry_has_is_reviewed(self):
        p = self._get()["proteins"][0]
        self.assertIn("is_reviewed", p)

    def test_reviewed_filter(self):
        # Flip EGFR to unreviewed; the review-status filter must partition the set.
        Protein.objects.filter(pk=self.egfr.pk).update(is_reviewed=False)
        rev = self._get(reviewed="reviewed")
        unrev = self._get(reviewed="unreviewed")
        self.assertNotIn(self.egfr.pk, [p["id"] for p in rev["proteins"]])
        self.assertEqual(unrev["total"], 1)
        self.assertEqual(unrev["proteins"][0]["id"], self.egfr.pk)


# ---------------------------------------------------------------------------
# 5. browse_filter_meta
# ---------------------------------------------------------------------------


class BrowseFilterMetaTest(HippieTestCase):
    def test_structure(self):
        r = self.client.get(reverse("hippie_website:browse_filter_meta"))
        data = json.loads(r.content)
        self.assertIn("tissues", data)
        self.assertIn("sources", data)
        self.assertIsInstance(data["tissues"], list)
        self.assertIsInstance(data["sources"], list)

    def test_source_present(self):
        data = json.loads(
            self.client.get(reverse("hippie_website:browse_filter_meta")).content
        )
        names = [s["name"] for s in data["sources"]]
        self.assertIn("BioGRID", names)


# ---------------------------------------------------------------------------
# 6. network_query (React shell + POST-JSON API)
# ---------------------------------------------------------------------------


class NetworkQueryTest(HippieTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.tissue = Tissue.objects.create(name="NetworkTestTissue")
        GeneTissue.objects.create(
            gene=cls.brca1.gene, tissue=cls.tissue, median_rpkm=1.0
        )
        GeneTissue.objects.create(
            gene=cls.tp53.gene, tissue=cls.tissue, median_rpkm=1.0
        )

    def _api(self, **body):
        return self.client.post(
            reverse("hippie_website:network_query_api"),
            data=json.dumps(body),
            content_type="application/json",
        )

    def test_get_renders_shell(self):
        r = self.client.get(reverse("hippie_website:network_query"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "hippie-nq-app")

    def test_post_with_valid_seeds(self):
        r = self._api(proteins="BRCA1\nTP53")
        self.assertEqual(r.status_code, 200)
        self.assertGreater(r.json()["edge_count"], 0)

    def test_seed_interaction_flag_for_within_set_edge(self):
        # BRCA1–TP53: both are seeds, so the edge is a seed interaction.
        data = self._api(proteins="BRCA1\nTP53").json()
        brca1_tp53 = [
            e
            for e in data["interactions"]
            if {e["a"]["symbol"], e["b"]["symbol"]} == {"BRCA1", "TP53"}
        ]
        self.assertTrue(brca1_tp53)
        self.assertTrue(all(e["seed_interaction"] for e in brca1_tp53))

    def test_min_rpkm_filter_on_partner(self):
        # Seed BRCA1 only; TP53 is the (non-seed) partner and must be expressed.
        base = dict(proteins="BRCA1", tissue=[self.tissue.pk])
        low = self._api(**base, min_rpkm=0.5).json()
        self.assertGreater(low["edge_count"], 0)
        high = self._api(**base, min_rpkm=2.0).json()
        self.assertEqual(high["edge_count"], 0)

    def test_unknown_seed_returns_error(self):
        r = self._api(proteins="FAKEPROT999")
        self.assertEqual(r.status_code, 400)
        self.assertIsNotNone(r.json()["error"])

    def test_unresolved_identifiers_reported(self):
        data = self._api(proteins="BRCA1\nFAKEPROT999").json()
        self.assertIn("FAKEPROT999", data["unresolved"])


# ---------------------------------------------------------------------------
# 7. ProteinQuerySet.resolve()
# ---------------------------------------------------------------------------


class ResolveTest(HippieTestCase):
    def test_resolve_by_symbol(self):
        qs = Protein.objects.resolve("BRCA1")
        self.assertEqual(qs.count(), 1)
        self.assertEqual(qs.first().gene.entrez_name, "BRCA1")

    def test_resolve_by_symbol_case_insensitive(self):
        qs = Protein.objects.resolve("brca1")
        self.assertEqual(qs.count(), 1)

    def test_resolve_by_entrez_id(self):
        qs = Protein.objects.resolve("672")
        self.assertEqual(qs.first().gene.entrez_name, "BRCA1")

    def test_resolve_by_uniprot_id(self):
        qs = Protein.objects.resolve("BRCA1_HUMAN")
        self.assertEqual(qs.first().gene.entrez_name, "BRCA1")

    def test_resolve_by_accession(self):
        qs = Protein.objects.resolve("P38398")
        self.assertEqual(qs.first().gene.entrez_name, "BRCA1")

    def test_resolve_isoform_keeps_isoform_uid(self):
        isoform = Isoform.objects.create(
            gene=self.brca1.gene,
            uniprot_name="BRCA1_2_HUMAN",
            uniprot_accession="P38398-2",
            general_protein=self.brca1,
        )
        qs = Protein.objects.resolve("P38398-2")
        self.assertEqual(qs.first().pk, isoform.pk)
        self.assertEqual(qs.first().uniprot_accession, "P38398-2")

    def test_resolve_unknown_returns_none_queryset(self):
        qs = Protein.objects.resolve("XXXXXXX")
        self.assertFalse(qs.exists())


# ---------------------------------------------------------------------------
# 8. Canonical Ordering
# ---------------------------------------------------------------------------


class CanonicalOrderingTest(HippieTestCase):
    def test_interaction_always_canonical(self):
        """protein_1_id muss immer <= protein_2_id sein."""
        a = make_protein("TESTA")
        b = make_protein("TESTB")
        ix = make_interaction(b, a)  # umgekehrt — make_interaction muss korrigieren
        self.assertLessEqual(ix.protein_1_id, ix.protein_2_id)

    def test_resolve_pair_finds_canonical(self):
        """_resolve_interaction_pair findet Interaction unabhängig von Reihenfolge."""
        result_ab = _resolve_interaction_pair("BRCA1", "TP53", 0)
        result_ba = _resolve_interaction_pair("TP53", "BRCA1", 0)
        self.assertEqual(result_ab["score"], result_ba["score"])
        self.assertEqual(result_ab["interaction_id"], result_ba["interaction_id"])


# ---------------------------------------------------------------------------
# 9. _protein_display() Helper
# ---------------------------------------------------------------------------


class ProteinDisplayTest(HippieTestCase):
    def test_all_keys_present(self):
        p = Protein.objects.select_related("gene").get(pk=self.brca1.pk)
        d = _protein_display(p)
        for key in ("id", "name", "symbol", "uniprot_id", "gene_id"):
            self.assertIn(key, d)

    def test_values_correct(self):
        p = Protein.objects.select_related("gene").get(pk=self.brca1.pk)
        d = _protein_display(p)
        self.assertEqual(d["name"], "BRCA1")
        self.assertEqual(d["symbol"], "BRCA1")
        self.assertEqual(d["uniprot_id"], "P38398")
        self.assertEqual(d["gene_id"], 672)

    def test_protein_without_mappings(self):
        """Protein ohne gene-Symbol und ohne accession soll nicht crashen."""
        bare = make_protein("BARE", accession="")
        bare_fetched = Protein.objects.select_related("gene").get(pk=bare.pk)
        d = _protein_display(bare_fetched)
        self.assertEqual(d["uniprot_id"], "")
        self.assertIsNone(d["gene_id"])
        self.assertEqual(d["symbol"], "BARE")  # fällt auf protein.name zurück


# ---------------------------------------------------------------------------
# 10. Detail-Views Kontext-Check
# ---------------------------------------------------------------------------


class ProteinQueryApiShowTest(HippieTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.ni = make_noninteraction(cls.brca1, cls.tp53, score=0.25)

    def _get(self, q, **params):
        r = self.client.get(
            reverse("hippie_website:protein_query_api"), {"q": q, **params}
        )
        self.assertEqual(r.status_code, 200)
        return json.loads(r.content)

    def test_show_noninteractions_returns_noninteraction_results(self):
        data = self._get("BRCA1", show="noninteractions")
        self.assertIsNone(data["error"])
        self.assertGreater(len(data["interactions"]), 0)
        for row in data["interactions"]:
            self.assertTrue(row["is_noninteraction"])

    def test_show_noninteractions_has_none_source_count(self):
        data = self._get("BRCA1", show="noninteractions")
        for row in data["interactions"]:
            self.assertIsNone(row["source_count"])
            self.assertIsNone(row["experiment_count"])

    def test_show_noninteractions_detail_url_uses_noninteraction(self):
        data = self._get("BRCA1", show="noninteractions")
        for row in data["interactions"]:
            self.assertIn("noninteraction", row["detail_url"])

    def test_show_both_contains_interaction_and_noninteraction(self):
        data = self._get("BRCA1", show="both")
        flags = {row["is_noninteraction"] for row in data["interactions"]}
        self.assertIn(True, flags)
        self.assertIn(False, flags)

    def test_show_both_sorted_by_score_descending(self):
        data = self._get("BRCA1", show="both")
        scores = [row["score"] for row in data["interactions"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_default_show_returns_only_interactions(self):
        # Default (no show param) should only return interactions
        data = self._get("BRCA1")
        for row in data["interactions"]:
            self.assertFalse(row["is_noninteraction"])


# ---------------------------------------------------------------------------
# 15. interaction_query_api — show parameter
# ---------------------------------------------------------------------------


class InteractionQueryApiShowNoninteractionTest(HippieTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.ni = make_noninteraction(cls.brca1, cls.tp53, score=0.25)

    def _post(self, pairs, extra=None, status=200):
        body = {"pairs": pairs}
        if extra:
            body.update(extra)
        r = self.client.post(
            reverse("hippie_website:interaction_query_api"),
            data=json.dumps(body),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, status)
        return json.loads(r.content)

    def test_show_noninteractions_known_pair(self):
        pairs = [{"a": "BRCA1", "b": "TP53", "input_order": 0}]
        data = self._post(pairs, {"show": "noninteractions"})
        self.assertEqual(len(data["results"]), 1)
        self.assertGreater(data["results"][0]["score"], 0)
        self.assertTrue(data["results"][0]["is_noninteraction"])

    def test_show_noninteractions_unknown_pair_returns_minus_one(self):
        pairs = [{"a": "BRCA1", "b": "EGFR", "input_order": 0}]
        data = self._post(pairs, {"show": "noninteractions"})
        self.assertEqual(data["results"][0]["score"], -1.0)

    def test_show_both_returns_interaction_and_noninteraction(self):
        # BRCA1–TP53 has both an Interaction and a NonInteraction
        pairs = [{"a": "BRCA1", "b": "TP53", "input_order": 0}]
        data = self._post(pairs, {"show": "both"})
        self.assertEqual(len(data["results"]), 2)
        flags = {r.get("is_noninteraction") for r in data["results"]}
        self.assertIn(True, flags)

    def test_show_both_only_interaction_when_no_noninteraction(self):
        new_p = make_protein("SHOW_BOTH_P", uniprot_name="SHOW_BOTH_H", gene_id=88001)
        make_interaction(self.brca1, new_p, score=0.5)
        pairs = [{"a": "BRCA1", "b": "SHOW_BOTH_P", "input_order": 0}]
        data = self._post(pairs, {"show": "both"})
        # Only the interaction is found; noninteraction returns -1 and is excluded
        scores = [r["score"] for r in data["results"]]
        self.assertIn(0.5, scores)


# ---------------------------------------------------------------------------
# 16. _protein_ids_from_raw
# ---------------------------------------------------------------------------


class ProteinIdsFromRawTest(HippieTestCase):
    def test_resolves_multiple(self):
        pks, unresolved = _protein_ids_from_raw("BRCA1 TP53")
        self.assertEqual(len(pks), 2)
        self.assertEqual(unresolved, [])

    def test_unresolved_identifiers_reported(self):
        pks, unresolved = _protein_ids_from_raw("BRCA1 FAKEPROT999")
        self.assertEqual(len(pks), 1)
        self.assertIn("FAKEPROT999", unresolved)

    def test_duplicate_identifier_deduplicated(self):
        pks, unresolved = _protein_ids_from_raw("BRCA1 BRCA1")
        self.assertEqual(len(pks), 1)

    def test_empty_string_returns_empty(self):
        pks, unresolved = _protein_ids_from_raw("")
        self.assertEqual(pks, [])
        self.assertEqual(unresolved, [])


# ---------------------------------------------------------------------------
# 17. _get_isoforms
# ---------------------------------------------------------------------------


class GetIsoformsTest(HippieTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # Isoform of BRCA1: accession P38398 → uniprot_accession starts with "P38398-"
        cls.isoform = Isoform.objects.create(
            gene=cls.brca1.gene,
            uniprot_name="",
            uniprot_accession="P38398-2",
            general_protein=cls.brca1,
        )

    def test_canonical_protein_returns_its_isoforms(self):
        isoforms = _get_isoforms(self.brca1.pk)
        iso_ids = [iso.uniprot_accession for iso in isoforms]
        self.assertIn("P38398-2", iso_ids)

    def test_isoform_itself_not_expanded_further(self):
        isoforms = _get_isoforms(self.isoform.pk)
        self.assertEqual(isoforms, [])

    def test_protein_without_uniprot_mapping_returns_empty(self):
        bare = make_protein("BARE_ISOTEST")
        self.assertEqual(_get_isoforms(bare.pk), [])

    def test_protein_without_accession_returns_empty(self):
        # Has UniProt entry ID but no accession mapping
        p = make_protein("NO_ACCESSION_P", uniprot_name="NOACC_HUMAN")
        self.assertEqual(_get_isoforms(p.pk), [])


# ---------------------------------------------------------------------------
# 17a. query_filters.isoform_only_q / parse_isoform_mode
# ---------------------------------------------------------------------------


class ParseIsoformModeTest(TestCase):
    def test_valid_modes_pass_through(self):
        for mode in ("general", "isoforms", "both"):
            self.assertEqual(parse_isoform_mode(mode), mode)

    def test_case_and_whitespace_insensitive(self):
        self.assertEqual(parse_isoform_mode(" Isoforms \n"), "isoforms")
        self.assertEqual(parse_isoform_mode("BOTH"), "both")

    def test_none_defaults_to_general(self):
        self.assertEqual(parse_isoform_mode(None), "general")

    def test_missing_or_garbage_falls_back_to_general(self):
        for garbage in ("", "bogus", "1", "true", "yes"):
            self.assertEqual(parse_isoform_mode(garbage), "general")


class IsoformOnlyQTest(HippieTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.isoform = Isoform.objects.create(
            gene=cls.brca1.gene,
            uniprot_name="",
            uniprot_accession="P38398-2",
            general_protein=cls.brca1,
        )
        cls.iso_ix = make_interaction(cls.isoform, cls.egfr, score=0.9)

    def test_keeps_only_edges_touching_an_isoform(self):
        qs = Interaction.objects.filter(isoform_only_q())
        pks = set(qs.values_list("pk", flat=True))
        self.assertIn(self.iso_ix.pk, pks)
        self.assertNotIn(self.ix.pk, pks)


# ---------------------------------------------------------------------------
# 17b. browse_interactions_api — denormalised involves_isoform flag
# ---------------------------------------------------------------------------


class BrowseInteractionsIsoformTest(HippieTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.isoform = Isoform.objects.create(
            gene=cls.brca1.gene,
            uniprot_name="",
            uniprot_accession="P38398-2",
            general_protein=cls.brca1,
        )
        # Interaction whose partner is an isoform.
        cls.iso_ix = make_interaction(cls.isoform, cls.egfr, score=0.9)
        recompute_flags()

    def test_flag_set_correctly(self):
        self.iso_ix.refresh_from_db()
        self.ix.refresh_from_db()
        self.assertTrue(self.iso_ix.involves_isoform)
        self.assertFalse(self.ix.involves_isoform)

    def test_default_excludes_isoform_interactions(self):
        r = self.client.get(reverse("hippie_website:browse_interactions_api"))
        ids = [row["id"] for row in r.json()["interactions"]]
        self.assertNotIn(self.iso_ix.pk, ids)
        self.assertIn(self.ix.pk, ids)  # canonical pair still present

    def test_include_isoforms_includes_them(self):
        r = self.client.get(
            reverse("hippie_website:browse_interactions_api"),
            {"isoform_mode": "both"},
        )
        ids = [row["id"] for row in r.json()["interactions"]]
        self.assertIn(self.iso_ix.pk, ids)

    def test_isoforms_only_excludes_canonical(self):
        r = self.client.get(
            reverse("hippie_website:browse_interactions_api"),
            {"isoform_mode": "isoforms"},
        )
        ids = [row["id"] for row in r.json()["interactions"]]
        self.assertIn(self.iso_ix.pk, ids)
        self.assertNotIn(self.ix.pk, ids)


# ---------------------------------------------------------------------------
# 18. InteractionQuerySet methods
# ---------------------------------------------------------------------------


class InteractionQuerySetMethodsTest(HippieTestCase):
    def test_between_proteins_returns_only_internal_edges(self):
        pks = [self.brca1.pk, self.tp53.pk]
        # Add an edge involving EGFR (not in the set)
        make_interaction(self.tp53, self.egfr, score=0.7)
        internal = Interaction.objects.all().between_proteins(pks)
        self.assertEqual(internal.count(), 1)
        ix = internal.first()
        self.assertIn(ix.protein_1_id, pks)
        self.assertIn(ix.protein_2_id, pks)

    def test_above_score_filters(self):
        qs_high = Interaction.objects.all().above_score(0.9)
        qs_low = Interaction.objects.all().above_score(0.5)
        # ix has score=0.85: passes 0.5 threshold, fails 0.9 threshold
        self.assertGreater(qs_low.count(), qs_high.count())
        self.assertEqual(qs_high.count(), 0)
        self.assertEqual(qs_low.count(), 1)

    def test_in_tissues_includes_when_both_expressed(self):
        tissue = Tissue.objects.create(name="Lung_test")
        GeneTissue.objects.create(gene=self.brca1.gene, tissue=tissue, median_rpkm=1.0)
        GeneTissue.objects.create(gene=self.tp53.gene, tissue=tissue, median_rpkm=1.0)
        qs = Interaction.objects.for_protein(self.brca1.pk).in_tissues([tissue.pk])
        self.assertEqual(qs.count(), 1)

    def test_in_tissues_excludes_when_one_side_not_expressed(self):
        tissue = Tissue.objects.create(name="Heart_test")
        # Only BRCA1 expressed; TP53 not
        GeneTissue.objects.create(gene=self.brca1.gene, tissue=tissue, median_rpkm=1.0)
        qs = Interaction.objects.for_protein(self.brca1.pk).in_tissues([tissue.pk])
        self.assertEqual(qs.count(), 0)

    def test_for_proteins_finds_edges_on_either_side(self):
        make_interaction(self.tp53, self.egfr, score=0.6)
        qs = Interaction.objects.for_proteins([self.egfr.pk])
        self.assertEqual(qs.count(), 1)


# ---------------------------------------------------------------------------
# 19. browse_api min_avg_score filter (unified CommonFilters param)
# ---------------------------------------------------------------------------


class BrowseApiMinScoreTest(HippieTestCase):
    def _get(self, **params):
        r = self.client.get(reverse("hippie_website:browse_api"), params)
        self.assertEqual(r.status_code, 200)
        return json.loads(r.content)

    def test_min_avg_score_excludes_proteins_with_low_avg(self):
        # BRCA1 + TP53 share a 0.85 interaction; EGFR has none (avg_score=None).
        data = self._get(min_avg_score=0.5)
        self.assertEqual(data["total"], 2)
        for p in data["proteins"]:
            self.assertIsNotNone(p["avg_score"])
            self.assertGreaterEqual(p["avg_score"], 0.5)

    def test_min_avg_score_zero_is_no_op(self):
        all_data = self._get()
        zero_data = self._get(min_avg_score=0)
        self.assertEqual(zero_data["total"], all_data["total"])


# ---------------------------------------------------------------------------
# 19b. browse_interactions_api
# ---------------------------------------------------------------------------


class BrowseInteractionsApiTest(HippieTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # brca1–tp53 is the interaction (cls.ix, one source + one experiment).
        # Add a non-interaction so the result-type toggle / union has both kinds.
        cls.ni = make_noninteraction(cls.brca1, cls.egfr, score=0.0)
        # Populate denormalised n_sources / n_experiments so count assertions
        # and count-column sorting exercise real values.
        recompute_flags()

    def _get(self, **params):
        r = self.client.get(reverse("hippie_website:browse_interactions_api"), params)
        self.assertEqual(r.status_code, 200)
        return json.loads(r.content)

    def test_returns_total_and_interactions(self):
        data = self._get()
        self.assertEqual(data["total"], Interaction.objects.count())
        self.assertGreater(len(data["interactions"]), 0)
        row = data["interactions"][0]
        for key in (
            "id",
            "protein_a",
            "protein_b",
            "score",
            "source_count",
            "experiment_count",
            "is_noninteraction",
            "detail_url",
        ):
            self.assertIn(key, row)
        self.assertFalse(row["is_noninteraction"])

    def test_rows_carry_review_status(self):
        # Each interactor now carries is_reviewed for the unreviewed tag /
        # Review Type export columns. Set brca1 reviewed, tp53 unreviewed
        # (the model default is False, so pin both sides explicitly).
        Protein.objects.filter(pk=self.brca1.pk).update(is_reviewed=True)
        Protein.objects.filter(pk=self.tp53.pk).update(is_reviewed=False)
        row = next(r for r in self._get()["interactions"] if not r["is_noninteraction"])
        self.assertIn("is_reviewed", row["protein_a"])
        self.assertIn("is_reviewed", row["protein_b"])
        # brca1–tp53 pair: one reviewed, one not (side order depends on pk).
        self.assertEqual(
            {row["protein_a"]["is_reviewed"], row["protein_b"]["is_reviewed"]},
            {True, False},
        )

    def test_score_range_filter(self):
        # The single fixture interaction scores 0.85.
        self.assertEqual(self._get(min_score=0.9)["total"], 0)
        self.assertEqual(self._get(min_score=0.5, max_score=0.9)["total"], 1)

    def test_source_filter(self):
        self.assertEqual(self._get(source=self.src.pk)["total"], 1)

    def test_experiment_filter(self):
        self.assertEqual(self._get(experiment=self.exp.pk)["total"], 1)

    def test_evidence_counts_denormalised(self):
        # Symmetric 1/1 counts couldn't catch a source/experiment column
        # swap in the response — give this interaction a second source so
        # the two counts differ.
        src2 = Source.objects.create(name="IntAct", url="https://www.ebi.ac.uk/intact/")
        self.ix.sources.add(src2)
        recompute_flags()

        row = self._get()["interactions"][0]
        self.assertEqual(row["source_count"], 2)
        self.assertEqual(row["experiment_count"], 1)

    def test_sort_by_sources_count(self):
        # sort=sources rides n_sources — previously untested. Give a second
        # interaction more sources than the shared brca1-tp53 fixture (1
        # source) and confirm both sort directions order by the count.
        src2 = Source.objects.create(name="IntAct", url="https://www.ebi.ac.uk/intact/")
        ix2 = make_interaction(self.tp53, self.egfr, score=0.5)
        ix2.sources.add(self.src, src2)
        recompute_flags()

        asc = self._get(sort="sources", dir="asc")
        self.assertEqual([r["id"] for r in asc["interactions"]], [self.ix.pk, ix2.pk])
        desc = self._get(sort="sources", dir="desc")
        self.assertEqual([r["id"] for r in desc["interactions"]], [ix2.pk, self.ix.pk])

    def test_sort_by_experiments_count(self):
        # sort=experiments rides n_experiments — previously untested.
        exp2 = ExperimentType.objects.create(
            name="Affinity Capture-MS", psi_mi_code="MI:0004", quality_score=3.0
        )
        ix2 = make_interaction(self.tp53, self.egfr, score=0.5)
        ix2.experiments.add(self.exp, exp2)
        recompute_flags()

        asc = self._get(sort="experiments", dir="asc")
        self.assertEqual([r["id"] for r in asc["interactions"]], [self.ix.pk, ix2.pk])
        desc = self._get(sort="experiments", dir="desc")
        self.assertEqual([r["id"] for r in desc["interactions"]], [ix2.pk, self.ix.pk])

    def test_pagination_tiebreak_across_union_kinds(self):
        # Interaction and NonInteraction have independent, overlapping id
        # sequences — force an explicit id collision between the two tables
        # (regression for the union pagination tiebreak fix) and confirm
        # offset/limit pagination is deterministic: no row dropped or
        # duplicated across the page boundary that lands on the tie.
        tied_id = 999999
        tied_ix = Interaction.objects.create(
            pk=tied_id, protein_1=self.tp53, protein_2=self.egfr, score=0.5
        )
        tied_ni = NonInteraction.objects.create(
            pk=tied_id, protein_1=self.tp53, protein_2=self.egfr, score=0.5
        )

        page1 = self._get(show="both", sort="score", dir="asc", offset=0, limit=2)
        page2 = self._get(show="both", sort="score", dir="asc", offset=2, limit=2)
        self.assertEqual(page1["total"], 4)

        seen = [(r["id"], r["is_noninteraction"]) for r in page1["interactions"]]
        seen += [(r["id"], r["is_noninteraction"]) for r in page2["interactions"]]
        self.assertEqual(len(seen), 4)
        self.assertEqual(len(set(seen)), 4)
        self.assertIn((tied_ix.pk, False), seen)
        self.assertIn((tied_ni.pk, True), seen)

    def test_show_noninteractions(self):
        data = self._get(show="noninteractions")
        self.assertEqual(data["total"], 1)
        row = data["interactions"][0]
        self.assertTrue(row["is_noninteraction"])
        self.assertEqual(row["source_count"], 0)
        self.assertIn("/noninteraction/", row["detail_url"])

    def test_show_both_unions_tables(self):
        data = self._get(show="both")
        self.assertEqual(data["total"], 2)
        kinds = sorted(r["is_noninteraction"] for r in data["interactions"])
        self.assertEqual(kinds, [False, True])

    def test_source_filter_excludes_noninteractions_in_both(self):
        # A source filter can never match a non-interaction → "both" collapses
        # to interactions only.
        data = self._get(show="both", source=self.src.pk)
        self.assertEqual(data["total"], 1)
        self.assertFalse(data["interactions"][0]["is_noninteraction"])

    def test_sort_by_symbol_a_does_not_error(self):
        # A third row with a protein_a symbol distinct from the shared
        # brca1-* fixtures makes this order-sensitive — with only the two
        # brca1-* rows, both canonicalize to the same protein_a and
        # `sorted(symbols) == symbols` passed unconditionally.
        make_noninteraction(self.tp53, self.egfr, score=0.0)

        data = self._get(show="both", sort="symbol_a", dir="asc")
        self.assertEqual(data["total"], 3)
        symbols = [r["protein_a"]["symbol"] for r in data["interactions"]]
        self.assertEqual(symbols, ["BRCA1", "BRCA1", "TP53"])


# ---------------------------------------------------------------------------
# 19c. browse_filter_meta — experiments for the interactions filter
# ---------------------------------------------------------------------------


class BrowseFilterMetaExperimentsTest(HippieTestCase):
    def test_experiments_present(self):
        data = json.loads(
            self.client.get(reverse("hippie_website:browse_filter_meta")).content
        )
        self.assertIn("experiments", data)
        names = [e["name"] for e in data["experiments"]]
        self.assertIn("Two-hybrid", names)


# ---------------------------------------------------------------------------
# 20. network_query filters
# ---------------------------------------------------------------------------


class NetworkQueryFilterTest(HippieTestCase):
    def _api(self, **body):
        return self.client.post(
            reverse("hippie_website:network_query_api"),
            data=json.dumps(body),
            content_type="application/json",
        )

    @staticmethod
    def _symbols(data):
        return {e["a"]["symbol"] for e in data["interactions"]} | {
            e["b"]["symbol"] for e in data["interactions"]
        }

    def test_min_score_above_threshold_returns_no_edges(self):
        # brca1–tp53 has score 0.85; min_score 0.99 excludes it.
        data = self._api(proteins="BRCA1\nTP53", min_score=0.99).json()
        self.assertEqual(data["edge_count"], 0)

    def test_first_shell_includes_seed_partner(self):
        # Seed BRCA1 → first-shell partner TP53 appears among endpoints.
        data = self._api(proteins="BRCA1").json()
        self.assertIn("TP53", self._symbols(data))

    def test_first_shell_expands_to_new_partner(self):
        # Add TP53–EGFR; seeding TP53 reaches EGFR (every edge touching a seed).
        make_interaction(self.tp53, self.egfr, score=0.7)
        data = self._api(proteins="TP53").json()
        self.assertIn("EGFR", self._symbols(data))

    def test_show_noninteractions(self):
        make_noninteraction(self.brca1, self.egfr, score=0.2)
        data = self._api(proteins="BRCA1", show="noninteractions").json()
        self.assertGreater(data["edge_count"], 0)
        self.assertTrue(all(e["is_noninteraction"] for e in data["interactions"]))


# ---------------------------------------------------------------------------
# 21. BaitPreyAssociation model
# ---------------------------------------------------------------------------


class ProteinDisplayIsoformTest(HippieTestCase):
    def _fetch(self, protein):
        return Protein.objects.select_related("gene").get(pk=protein.pk)

    def test_isoform_uniprot_id_key_present(self):
        d = _protein_display(self._fetch(self.brca1))
        self.assertIn("isoform_uniprot_id", d)

    def test_isoform_uniprot_id_none_for_canonical(self):
        d = _protein_display(self._fetch(self.brca1))
        self.assertIsNone(d["isoform_uniprot_id"])

    def test_isoform_uid_param_returned(self):
        d = _protein_display(self._fetch(self.brca1), isoform_uid="P38398-2")
        self.assertEqual(d["isoform_uniprot_id"], "P38398-2")

    def test_isoform_uid_none_does_not_override(self):
        # Explicitly passing None should fall back to getattr on the protein
        d = _protein_display(self._fetch(self.brca1), isoform_uid=None)
        self.assertIsNone(d["isoform_uniprot_id"])


# ---------------------------------------------------------------------------
# 23. _safe_int / _safe_float helpers
# ---------------------------------------------------------------------------


class SafeConversionTest(TestCase):
    def test_safe_int_none(self):
        self.assertIsNone(_safe_int(None))

    def test_safe_int_empty_string(self):
        self.assertIsNone(_safe_int(""))

    def test_safe_int_non_numeric(self):
        self.assertIsNone(_safe_int("abc"))

    def test_safe_int_valid_string(self):
        self.assertEqual(_safe_int("42"), 42)

    def test_safe_int_valid_int(self):
        self.assertEqual(_safe_int(7), 7)

    def test_safe_float_none(self):
        self.assertIsNone(_safe_float(None))

    def test_safe_float_empty_string(self):
        self.assertIsNone(_safe_float(""))

    def test_safe_float_non_numeric(self):
        self.assertIsNone(_safe_float("xyz"))

    def test_safe_float_valid_string(self):
        self.assertAlmostEqual(_safe_float("0.75"), 0.75)

    def test_safe_float_valid_float(self):
        self.assertAlmostEqual(_safe_float(0.5), 0.5)


class Batch3ProteinQueryFilterTest(HippieTestCase):
    """protein_query_api now honours the full shared filter set; protein-level
    filters (score/source/experiment/reviewed) apply to the partner (B) side."""

    def _query(self, q, **params):
        r = self.client.get(
            reverse("hippie_website:protein_query_api"), {"q": q, **params}
        )
        self.assertEqual(r.status_code, 200)
        return json.loads(r.content)

    def test_source_filter_limits_partners(self):
        # brca1–tp53 carries BioGRID; add brca1–egfr with no source at all.
        make_interaction(self.brca1, self.egfr, score=0.9)
        self.assertEqual(len(self._query("BRCA1")["interactions"]), 2)
        only_src = self._query("BRCA1", source=self.src.pk)
        partners = {i["partner"]["symbol"] for i in only_src["interactions"]}
        self.assertEqual(partners, {"TP53"})

    def test_experiment_filter_limits_partners(self):
        make_interaction(self.brca1, self.egfr, score=0.9)  # no experiment
        rows = self._query("BRCA1", experiment=self.exp.pk)
        partners = {i["partner"]["symbol"] for i in rows["interactions"]}
        self.assertEqual(partners, {"TP53"})

    def test_min_score_filter(self):
        make_interaction(self.brca1, self.egfr, score=0.9)
        rows = self._query("BRCA1", min_score=0.88)["interactions"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(all(i["score"] >= 0.88 for i in rows))

    def test_reviewed_unreviewed_excludes_all_partners(self):
        # Every fixture protein defaults is_reviewed=True → unreviewed → empty.
        self.assertEqual(
            len(self._query("BRCA1", reviewed="unreviewed")["interactions"]), 0
        )
        self.assertGreaterEqual(
            len(self._query("BRCA1", reviewed="reviewed")["interactions"]), 1
        )

    def test_max_score_filter(self):
        make_interaction(self.brca1, self.egfr, score=0.9)
        rows = self._query("BRCA1", max_score=0.87)["interactions"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(all(i["score"] <= 0.87 for i in rows))

    def test_min_degree_filter(self):
        # tp53's denormalised degree is 1 (its only interaction, with brca1,
        # existed when recompute_protein_stats last ran); egfr's is 0.
        make_interaction(self.brca1, self.egfr, score=0.9)
        rows = self._query("BRCA1", min_degree=1)["interactions"]
        partners = {i["partner"]["symbol"] for i in rows}
        self.assertEqual(partners, {"TP53"})

    def test_min_avg_score_filter(self):
        # tp53's denormalised avg_score is 0.85; egfr's is None (it had no
        # interactions when recompute_protein_stats last ran).
        make_interaction(self.brca1, self.egfr, score=0.9)
        rows = self._query("BRCA1", min_avg_score=0.5)["interactions"]
        partners = {i["partner"]["symbol"] for i in rows}
        self.assertEqual(partners, {"TP53"})

    def test_tissue_filter(self):
        tissue = Tissue.objects.create(name="Brain")
        GeneTissue.objects.create(gene=self.tp53.gene, tissue=tissue, median_rpkm=1.0)
        make_interaction(self.brca1, self.egfr, score=0.9)
        rows = self._query("BRCA1", tissue=tissue.pk, min_rpkm=0.5)["interactions"]
        partners = {i["partner"]["symbol"] for i in rows}
        self.assertEqual(partners, {"TP53"})

    def test_interaction_type_filter_limits_partners(self):
        from ..models import InteractionType

        itype = InteractionType.objects.create(
            name="direct interaction", psi_mi_code="MI:0407"
        )
        self.ix.interaction_types.add(itype)
        make_interaction(self.brca1, self.egfr, score=0.9)  # no interaction_type
        rows = self._query("BRCA1", interaction_type=itype.pk)["interactions"]
        partners = {i["partner"]["symbol"] for i in rows}
        self.assertEqual(partners, {"TP53"})

    def test_noninteractions_excluded_when_source_like_filter_active(self):
        # NonInteractions carry no sources, so a source filter must exclude
        # them entirely rather than silently including every non-interaction.
        make_noninteraction(self.brca1, self.egfr, score=0.3)
        rows = self._query("BRCA1", show="both", source=self.src.pk)["interactions"]
        self.assertTrue(all(not i["is_noninteraction"] for i in rows))
        partners = {i["partner"]["symbol"] for i in rows}
        self.assertEqual(partners, {"TP53"})


class Batch3InteractionQueryFilterTest(HippieTestCase):
    """interaction_query_api now returns entrez + is_noninteraction and applies
    the full filter set; a match that fails a filter becomes a not-found row
    (score -1) so every input pair still yields exactly one row."""

    def _post(self, pairs, **body):
        r = self.client.post(
            reverse("hippie_website:interaction_query_api"),
            data=json.dumps({"pairs": pairs, **body}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        return json.loads(r.content)["results"]

    def test_entrez_and_type_fields_present(self):
        r = self._post([{"a": "BRCA1", "b": "TP53", "input_order": 0}])[0]
        self.assertEqual(r["entrez_a"], 672)
        self.assertEqual(r["entrez_b"], 7157)
        self.assertFalse(r["is_noninteraction"])

    def test_not_found_pair_has_null_entrez(self):
        r = self._post([{"a": "FAKEPROT", "b": "TP53", "input_order": 0}])[0]
        self.assertIsNone(r["entrez_a"])

    def test_min_score_miss_becomes_not_found_row(self):
        # brca1–tp53 = 0.85; min_score 0.9 filters it out → one not-found row.
        results = self._post(
            [{"a": "BRCA1", "b": "TP53", "input_order": 0}], min_score=0.9
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["score"], -1.0)

    def test_source_filter_match_and_miss(self):
        hit = self._post(
            [{"a": "BRCA1", "b": "TP53", "input_order": 0}], source=[self.src.pk]
        )
        self.assertGreater(hit[0]["score"], 0)
        other = Source.objects.create(name="OtherDB", url="")
        miss = self._post(
            [{"a": "BRCA1", "b": "TP53", "input_order": 0}], source=[other.pk]
        )
        self.assertEqual(miss[0]["score"], -1.0)

    def test_isoform_both_noninteraction_yields_single_row(self):
        # A pair that is ONLY a documented non-interaction must yield exactly one
        # row in isoform + both mode — no spurious not-found (-1) row alongside it.
        make_noninteraction(self.brca1, self.egfr, score=0.3)
        results = self._post(
            [{"a": "BRCA1", "b": "EGFR", "input_order": 0}],
            show="both",
            isoform_mode="both",
        )
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["is_noninteraction"])
        self.assertGreaterEqual(results[0]["score"], 0)

    def test_isoform_isoforms_noninteraction_yields_single_row(self):
        # Same pair, "isoforms" mode: no isoform-involving Interaction exists
        # (the not-found fallback), but the plain canonical NonInteraction is
        # still found (NonInteraction isn't isoform-expanded) — still exactly
        # one row, not two.
        make_noninteraction(self.brca1, self.egfr, score=0.3)
        results = self._post(
            [{"a": "BRCA1", "b": "EGFR", "input_order": 0}],
            show="both",
            isoform_mode="isoforms",
        )
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["is_noninteraction"])
        self.assertGreaterEqual(results[0]["score"], 0)

    def test_max_score_filter(self):
        # brca1-tp53 = 0.85; max_score 0.5 excludes it → not-found row.
        miss = self._post(
            [{"a": "BRCA1", "b": "TP53", "input_order": 0}], max_score=0.5
        )
        self.assertEqual(miss[0]["score"], -1.0)
        hit = self._post([{"a": "BRCA1", "b": "TP53", "input_order": 0}], max_score=0.9)
        self.assertGreater(hit[0]["score"], 0)

    def test_min_score_hit_returns_found_row(self):
        # A threshold below the fixture's actual score must still return a match.
        results = self._post(
            [{"a": "BRCA1", "b": "TP53", "input_order": 0}], min_score=0.5
        )
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["is_noninteraction"])
        self.assertGreaterEqual(results[0]["score"], 0.5)

    def test_experiment_filter_match_and_miss(self):
        hit = self._post(
            [{"a": "BRCA1", "b": "TP53", "input_order": 0}], experiment=[self.exp.pk]
        )
        self.assertGreater(hit[0]["score"], 0)
        other = ExperimentType.objects.create(
            name="Affinity chromatography", psi_mi_code="MI:0004", quality_score=3.0
        )
        miss = self._post(
            [{"a": "BRCA1", "b": "TP53", "input_order": 0}], experiment=[other.pk]
        )
        self.assertEqual(miss[0]["score"], -1.0)

    def test_reviewed_filter(self):
        Protein.objects.filter(pk=self.egfr.pk).update(is_reviewed=False)
        make_interaction(self.brca1, self.egfr, score=0.9)
        hit = self._post(
            [{"a": "BRCA1", "b": "TP53", "input_order": 0}], reviewed="reviewed"
        )
        self.assertGreater(hit[0]["score"], 0)
        miss = self._post(
            [{"a": "BRCA1", "b": "EGFR", "input_order": 0}], reviewed="reviewed"
        )
        self.assertEqual(miss[0]["score"], -1.0)

    def test_min_degree_filter(self):
        # egfr's denormalised degree is 0 (it had no interactions when
        # recompute_protein_stats last ran).
        make_interaction(self.brca1, self.egfr, score=0.9)
        hit = self._post([{"a": "BRCA1", "b": "TP53", "input_order": 0}], min_degree=1)
        self.assertGreater(hit[0]["score"], 0)
        miss = self._post([{"a": "BRCA1", "b": "EGFR", "input_order": 0}], min_degree=1)
        self.assertEqual(miss[0]["score"], -1.0)

    def test_min_avg_score_filter(self):
        make_interaction(self.brca1, self.egfr, score=0.9)
        hit = self._post(
            [{"a": "BRCA1", "b": "TP53", "input_order": 0}], min_avg_score=0.5
        )
        self.assertGreater(hit[0]["score"], 0)
        # egfr's denormalised avg_score is None → fails the threshold.
        miss = self._post(
            [{"a": "BRCA1", "b": "EGFR", "input_order": 0}], min_avg_score=0.5
        )
        self.assertEqual(miss[0]["score"], -1.0)

    def test_tissue_filter(self):
        # Interaction-level tissue filtering requires BOTH endpoints to be
        # expressed in the tissue, unlike the partner-only check used by
        # protein_query_api.
        tissue = Tissue.objects.create(name="Brain")
        GeneTissue.objects.create(gene=self.brca1.gene, tissue=tissue, median_rpkm=1.0)
        GeneTissue.objects.create(gene=self.tp53.gene, tissue=tissue, median_rpkm=1.0)
        hit = self._post(
            [{"a": "BRCA1", "b": "TP53", "input_order": 0}],
            tissue=[tissue.pk],
            min_rpkm=0.5,
        )
        self.assertGreater(hit[0]["score"], 0)
        # egfr's gene has no tissue expression recorded → fails the filter.
        make_interaction(self.brca1, self.egfr, score=0.9)
        miss = self._post(
            [{"a": "BRCA1", "b": "EGFR", "input_order": 0}],
            tissue=[tissue.pk],
            min_rpkm=0.5,
        )
        self.assertEqual(miss[0]["score"], -1.0)

    def test_interaction_type_filter_match_and_miss(self):
        from ..models import InteractionType

        itype = InteractionType.objects.create(
            name="direct interaction", psi_mi_code="MI:0407"
        )
        self.ix.interaction_types.add(itype)
        hit = self._post(
            [{"a": "BRCA1", "b": "TP53", "input_order": 0}],
            interaction_type=[itype.pk],
        )
        self.assertGreater(hit[0]["score"], 0)
        other = InteractionType.objects.create(
            name="physical association", psi_mi_code="MI:0915"
        )
        miss = self._post(
            [{"a": "BRCA1", "b": "TP53", "input_order": 0}],
            interaction_type=[other.pk],
        )
        self.assertEqual(miss[0]["score"], -1.0)

    def test_noninteraction_excluded_by_source_like_filter(self):
        # NonInteractions carry no sources; a source filter must exclude them
        # rather than treating the has_source_like check as a no-op.
        make_noninteraction(self.brca1, self.egfr, score=0.3)
        results = self._post(
            [{"a": "BRCA1", "b": "EGFR", "input_order": 0}],
            show="noninteractions",
            source=[self.src.pk],
        )
        self.assertEqual(results[0]["score"], -1.0)

    def test_scalar_sent_as_list_and_list_sent_as_scalar(self):
        # min_score (a scalar field) sent as a single-element list, and source
        # (a list field) sent as a bare scalar — the JSON-body adapter must
        # unwrap/wrap these the same way the GET adapter's querystring
        # semantics do (repeated keys vs. a single value).
        results = self._post(
            [{"a": "BRCA1", "b": "TP53", "input_order": 0}],
            min_score=[0.5],
            source=self.src.pk,
        )
        self.assertEqual(len(results), 1)
        self.assertGreaterEqual(results[0]["score"], 0.5)

    def test_isoform_expansion_applies_protein_level_filter_per_combo(self):
        iso_ok = Isoform.objects.create(
            gene=self.brca1.gene,
            uniprot_name="",
            uniprot_accession="P38398-2",
            general_protein=self.brca1,
            is_reviewed=True,
        )
        iso_bad = Isoform.objects.create(
            gene=self.brca1.gene,
            uniprot_name="",
            uniprot_accession="P38398-3",
            general_protein=self.brca1,
        )
        Protein.objects.filter(pk=iso_bad.pk).update(is_reviewed=False)
        make_interaction(iso_ok, self.tp53, score=0.7)
        make_interaction(iso_bad, self.tp53, score=0.6)

        results = self._post(
            [{"a": "BRCA1", "b": "TP53", "input_order": 0}],
            isoform_mode="both",
            reviewed="reviewed",
        )
        isoform_tags = {r["isoform_uniprot_a"] for r in results}
        self.assertIn("P38398-2", isoform_tags)
        self.assertNotIn("P38398-3", isoform_tags)

    def test_isoform_mode_isoforms_drops_canonical_combo(self):
        iso_ok = Isoform.objects.create(
            gene=self.brca1.gene,
            uniprot_name="",
            uniprot_accession="P38398-2",
            general_protein=self.brca1,
            is_reviewed=True,
        )
        make_interaction(iso_ok, self.tp53, score=0.7)

        results = self._post(
            [{"a": "BRCA1", "b": "TP53", "input_order": 0}],
            isoform_mode="isoforms",
        )
        # Only the isoform-involving combo is returned — the pure canonical
        # BRCA1-TP53 combo (isoform_uniprot_a is None) is dropped.
        isoform_tags = {r["isoform_uniprot_a"] for r in results}
        self.assertEqual(isoform_tags, {"P38398-2"})


class Batch3FilterMetaInteractionTypesTest(HippieTestCase):
    def test_interaction_types_returned(self):
        from ..models import InteractionType

        InteractionType.objects.create(name="direct interaction", psi_mi_code="MI:0407")
        r = self.client.get(reverse("hippie_website:browse_filter_meta"))
        data = json.loads(r.content)
        self.assertIn("interaction_types", data)
        names = [x["name"] for x in data["interaction_types"]]
        self.assertIn("direct interaction", names)


# ---------------------------------------------------------------------------
# Batch 6 — ML Splits: tissue coverage over survivors, status queue position
# ---------------------------------------------------------------------------
