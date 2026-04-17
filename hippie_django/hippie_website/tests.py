"""
Tests für HIPPIE Django.

Abdeckung:
  - Alle URL-Endpunkte (HTTP-Status)
  - protein_query_api: Auflösung nach Symbol, UniProt-ID, Accession, Entrez-ID
  - interaction_query_api: bekanntes Pair, unbekanntes Pair, fehlendes Protein, zu großer Batch
  - browse_api: Grundstruktur, Tissue-Filter, Source-Filter, Min-Degree-Filter
  - browse_filter_meta: Struktur der Antwort
  - network_query_view: POST mit Seeds
  - protein_detail_view / interaction_detail_view: 200 und 404
  - ProteinQuerySet.resolve(): alle vier Identifier-Typen
  - Canonical ordering in Interaction
  - _protein_display() Helper
"""

import json
from django.test import TestCase, Client
from django.urls import reverse

from .models import (
    Interaction,
    Protein,
    ProteinEntrez,
    ProteinUniProt,
    Source,
    ExperimentType,
    Tissue,
    ProteinTissue,
    UniProtAccession,
)
from .views import _protein_display, _resolve_interaction_pair


# ---------------------------------------------------------------------------
# Fixtures — wiederverwendbare Testdaten
# ---------------------------------------------------------------------------


def make_protein(name, uniprot_id=None, gene_id=None, accession=None):
    """Erstellt ein Protein mit optionalen Identifier-Mappings."""
    p = Protein.objects.create(name=name)
    if uniprot_id:
        ProteinUniProt.objects.create(protein=p, uniprot_id=uniprot_id, version=1)
    if gene_id:
        ProteinEntrez.objects.create(protein=p, gene_id=gene_id, name=name)
    if accession and uniprot_id:
        UniProtAccession.objects.create(accession=accession, uniprot_id=uniprot_id)
    return p


def make_interaction(p1, p2, score=0.8):
    """Erstellt eine Interaction in kanonischer Reihenfolge."""
    a, b = (p1, p2) if p1.pk <= p2.pk else (p2, p1)
    return Interaction.objects.create(protein_1=a, protein_2=b, score=score)


# ---------------------------------------------------------------------------
# Basis-Testklasse mit gemeinsamen Fixtures
# ---------------------------------------------------------------------------


class HippieTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.brca1 = make_protein(
            "BRCA1", uniprot_id="BRCA1_HUMAN", gene_id=672, accession="P38398"
        )
        cls.tp53 = make_protein(
            "TP53", uniprot_id="P53_HUMAN", gene_id=7157, accession="P04637"
        )
        cls.egfr = make_protein(
            "EGFR", uniprot_id="EGFR_HUMAN", gene_id=1956, accession="P00533"
        )
        cls.ix = make_interaction(cls.brca1, cls.tp53, score=0.85)
        cls.src = Source.objects.create(name="BioGRID", url="https://thebiogrid.org/")
        cls.exp = ExperimentType.objects.create(
            name="Two-hybrid", psi_mi_code="MI:0018", quality_score=5.0
        )
        cls.ix.sources.add(cls.src)
        cls.ix.experiments.add(cls.exp)
        cls.client = Client()


# ---------------------------------------------------------------------------
# 1. URL-Smoke-Tests — jeder Endpunkt muss erreichbar sein
# ---------------------------------------------------------------------------


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

    def test_protein_detail_get(self):
        r = self.client.get(
            reverse("hippie_website:protein_detail", args=[self.brca1.pk])
        )
        self.assertEqual(r.status_code, 200)

    def test_protein_detail_404(self):
        r = self.client.get(reverse("hippie_website:protein_detail", args=[99999]))
        self.assertEqual(r.status_code, 404)

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
        ProteinTissue.objects.create(protein=cls.brca1, tissue=cls.tissue)

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

    def test_source_filter(self):
        data = self._get(source=self.src.pk)
        # BRCA1 und TP53 haben eine Interaction mit BioGRID als Source
        self.assertGreater(data["total"], 0)

    def test_min_degree_filter(self):
        data = self._get(min_degree=1)
        # Nur Proteine mit mindestens einer Interaction
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
# 6. network_query (POST via Form)
# ---------------------------------------------------------------------------


class NetworkQueryTest(HippieTestCase):
    def test_get_renders_form(self):
        r = self.client.get(reverse("hippie_website:network_query"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "hippie-hero")

    def test_post_with_valid_seeds(self):
        r = self.client.post(
            reverse("hippie_website:network_query"),
            {
                "proteins": "BRCA1\nTP53",
                "output_type": "browser_vis",
                "layer_0": "on",
                "score_min": "0.0",
                "direction": "none",
                "effect": "none",
            },
        )
        self.assertEqual(r.status_code, 200)
        ctx = r.context
        self.assertIsNotNone(ctx["network_result"])
        self.assertGreater(ctx["network_result"]["edge_count"], 0)

    def test_post_with_unknown_seed(self):
        r = self.client.post(
            reverse("hippie_website:network_query"),
            {
                "proteins": "FAKEPROT999",
                "output_type": "browser_vis",
                "direction": "none",
                "effect": "none",
            },
        )
        self.assertEqual(r.status_code, 200)
        ctx = r.context
        self.assertIsNotNone(ctx["network_result"]["error"])

    def test_post_unresolved_identifiers_reported(self):
        r = self.client.post(
            reverse("hippie_website:network_query"),
            {
                "proteins": "BRCA1\nFAKEPROT999",
                "output_type": "browser_vis",
                "layer_0": "on",
                "direction": "none",
                "effect": "none",
            },
        )
        ctx = r.context
        self.assertIn("FAKEPROT999", ctx["network_result"]["unresolved"])


# ---------------------------------------------------------------------------
# 7. ProteinQuerySet.resolve()
# ---------------------------------------------------------------------------


class ResolveTest(HippieTestCase):
    def test_resolve_by_symbol(self):
        qs = Protein.objects.resolve("BRCA1")
        self.assertEqual(qs.count(), 1)
        self.assertEqual(qs.first().name, "BRCA1")

    def test_resolve_by_symbol_case_insensitive(self):
        qs = Protein.objects.resolve("brca1")
        self.assertEqual(qs.count(), 1)

    def test_resolve_by_entrez_id(self):
        qs = Protein.objects.resolve("672")
        self.assertEqual(qs.first().name, "BRCA1")

    def test_resolve_by_uniprot_id(self):
        qs = Protein.objects.resolve("BRCA1_HUMAN")
        self.assertEqual(qs.first().name, "BRCA1")

    def test_resolve_by_accession(self):
        qs = Protein.objects.resolve("P38398")
        self.assertEqual(qs.first().name, "BRCA1")

    def test_resolve_unknown_returns_none_queryset(self):
        qs = Protein.objects.resolve("XXXXXXX")
        self.assertFalse(qs.exists())


# ---------------------------------------------------------------------------
# 8. Canonical Ordering
# ---------------------------------------------------------------------------


class CanonicalOrderingTest(HippieTestCase):
    def test_interaction_always_canonical(self):
        """protein_1_id muss immer <= protein_2_id sein."""
        a = Protein.objects.create(name="TESTA")
        b = Protein.objects.create(name="TESTB")
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
        p = Protein.objects.prefetch_related("uniprot_ids", "entrez_ids").get(
            pk=self.brca1.pk
        )
        d = _protein_display(p)
        for key in ("id", "name", "symbol", "uniprot_id", "gene_id"):
            self.assertIn(key, d)

    def test_values_correct(self):
        p = Protein.objects.prefetch_related("uniprot_ids", "entrez_ids").get(
            pk=self.brca1.pk
        )
        d = _protein_display(p)
        self.assertEqual(d["name"], "BRCA1")
        self.assertEqual(d["symbol"], "BRCA1")
        self.assertEqual(d["uniprot_id"], "BRCA1_HUMAN")
        self.assertEqual(d["gene_id"], 672)

    def test_protein_without_mappings(self):
        """Protein ohne UniProt/Entrez soll nicht crashen."""
        bare = Protein.objects.create(name="BARE")
        bare_fetched = Protein.objects.prefetch_related(
            "uniprot_ids", "entrez_ids"
        ).get(pk=bare.pk)
        d = _protein_display(bare_fetched)
        self.assertEqual(d["uniprot_id"], "")
        self.assertIsNone(d["gene_id"])
        self.assertEqual(d["symbol"], "BARE")  # fällt auf protein.name zurück


# ---------------------------------------------------------------------------
# 10. Detail-Views Kontext-Check
# ---------------------------------------------------------------------------


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

    def test_protein_detail_context_keys(self):
        r = self.client.get(
            reverse("hippie_website:protein_detail", args=[self.brca1.pk])
        )
        ctx = r.context
        for key in ("protein", "symbol", "uniprot_id", "gene_id", "interaction_count"):
            self.assertIn(key, ctx)

    def test_protein_detail_interaction_count(self):
        r = self.client.get(
            reverse("hippie_website:protein_detail", args=[self.brca1.pk])
        )
        self.assertEqual(r.context["interaction_count"], 1)
