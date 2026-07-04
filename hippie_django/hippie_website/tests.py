"""
Tests für HIPPIE Django.

Abdeckung:
  - Alle URL-Endpunkte (HTTP-Status)
  - protein_query_api: Auflösung nach Symbol, UniProt-ID, Accession, Entrez-ID
  - interaction_query_api: bekanntes Pair, unbekanntes Pair, fehlendes Protein, zu großer Batch
  - browse_api: Grundstruktur, Tissue-Filter, Source-Filter, Min-Degree-Filter, Min-Score-Filter, Min-RPKM-Filter
  - browse_filter_meta: Struktur der Antwort
  - network_query_view: POST mit Seeds, Score-Filter, Layer-0-Filter, Min-RPKM-Filter
  - interaction_detail_view / noninteraction_detail_view: 200 und 404
  - ProteinQuerySet.resolve(): alle vier Identifier-Typen
  - Canonical ordering in Interaction und NonInteraction
  - _protein_display() Helper (inkl. isoform_uid Parameter)
  - _resolve_noninteraction_pair(): bekanntes Pair, unbekanntes, kein NonInteraction
  - _protein_ids_from_raw(): Auflösung, Deduplizierung, Unresolved
  - _get_isoforms(): Isoform-Expansion
  - protein_query_api show=noninteractions / show=both
  - interaction_query_api show=noninteractions / show=both
  - InteractionQuerySet: between_proteins, above_score, in_tissues
  - BaitPreyAssociation Modell
  - _safe_int / _safe_float Helpers
"""

import json
import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import CommandError, call_command
from django.test import TestCase, Client, SimpleTestCase
from django.urls import reverse

from .models import (
    BaitPreyAssociation,
    Gene,
    Interaction,
    Isoform,
    NonInteraction,
    Protein,
    Publication,
    Source,
    ExperimentType,
    Tissue,
    GeneTissue,
)
from .views import (
    _protein_display,
    _resolve_interaction_pair,
    _resolve_noninteraction_pair,
    _protein_ids_from_raw,
    _get_isoforms,
    _safe_int,
    _safe_float,
)


# ---------------------------------------------------------------------------
# Fixtures — wiederverwendbare Testdaten
# ---------------------------------------------------------------------------


def make_protein(name, uniprot_name=None, gene_id=None, accession=None):
    """Erstellt ein Protein mit optionalen Identifier-Mappings."""
    if gene_id is not None:
        gene, _ = Gene.objects.get_or_create(
            entrez_id=gene_id, defaults={"entrez_name": name}
        )
    else:
        gene, _ = Gene.objects.get_or_create(entrez_id=0, defaults={"entrez_name": ""})
    return Protein.objects.create(
        gene=gene,
        uniprot_name=uniprot_name or (name if gene_id is None else ""),
        uniprot_accession=accession if accession is not None else f"TEST_{name}",
    )


def make_interaction(p1, p2, score=0.8):
    """Erstellt eine Interaction in kanonischer Reihenfolge."""
    a, b = (p1, p2) if p1.pk <= p2.pk else (p2, p1)
    return Interaction.objects.create(protein_1=a, protein_2=b, score=score)


def make_noninteraction(p1, p2, score=0.3):
    """Erstellt eine NonInteraction in kanonischer Reihenfolge."""
    a, b = (p1, p2) if p1.pk <= p2.pk else (p2, p1)
    return NonInteraction.objects.create(protein_1=a, protein_2=b, score=score)


# ---------------------------------------------------------------------------
# Basis-Testklasse mit gemeinsamen Fixtures
# ---------------------------------------------------------------------------


class HippieTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.brca1 = make_protein(
            "BRCA1", uniprot_name="BRCA1_HUMAN", gene_id=672, accession="P38398"
        )
        cls.tp53 = make_protein(
            "TP53", uniprot_name="P53_HUMAN", gene_id=7157, accession="P04637"
        )
        cls.egfr = make_protein(
            "EGFR", uniprot_name="EGFR_HUMAN", gene_id=1956, accession="P00533"
        )
        cls.ix = make_interaction(cls.brca1, cls.tp53, score=0.85)
        cls.src = Source.objects.create(
            name="BioGRID", url="https://thebiogrid.org/", n_connected_interactions=1
        )
        cls.exp = ExperimentType.objects.create(
            name="Two-hybrid", psi_mi_code="MI:0018", quality_score=5.0
        )
        cls.ix.sources.add(cls.src)
        cls.ix.experiments.add(cls.exp)
        # Browse reads denormalised degree/avg_score columns; populate them so
        # min_degree / min_score filter tests exercise real values.
        call_command("recompute_protein_stats", stdout=StringIO())
        cls.client = Client()

    def setUp(self):
        # Browse totals are memoised in the (process-global) cache; clear it
        # between tests so a cached count from one test can't leak into another.
        from django.core.cache import cache

        cache.clear()


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

    def test_protein_entry_has_is_swissprot(self):
        p = self._get()["proteins"][0]
        self.assertIn("is_swissprot", p)

    def test_swissprot_filter(self):
        # Flip EGFR to TrEMBL; the review-status filter must partition the set.
        Protein.objects.filter(pk=self.egfr.pk).update(is_swissprot=False)
        sp = self._get(swissprot="swissprot")
        tr = self._get(swissprot="trembl")
        self.assertNotIn(self.egfr.pk, [p["id"] for p in sp["proteins"]])
        self.assertEqual(tr["total"], 1)
        self.assertEqual(tr["proteins"][0]["id"], self.egfr.pk)


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
                "negatome_edges": "none",
            },
        )
        self.assertEqual(r.status_code, 200)
        ctx = r.context
        self.assertIsNotNone(ctx["network_result"])
        self.assertGreater(ctx["network_result"]["edge_count"], 0)

    def test_min_rpkm_filter(self):
        base = {
            "proteins": "BRCA1\nTP53",
            "output_type": "browser_vis",
            "layer_0": "on",
            "score_min": "0.0",
            "direction": "none",
            "effect": "none",
            "negatome_edges": "none",
            "tissue": self.tissue.pk,
        }
        # both proteins have rpkm=1.0; threshold below → edge survives
        r_low = self.client.post(
            reverse("hippie_website:network_query"), {**base, "min_rpkm": "0.5"}
        )
        self.assertGreater(r_low.context["network_result"]["edge_count"], 0)
        # threshold above → edge filtered out
        r_high = self.client.post(
            reverse("hippie_website:network_query"), {**base, "min_rpkm": "2.0"}
        )
        self.assertEqual(r_high.context["network_result"]["edge_count"], 0)

    def test_post_with_unknown_seed(self):
        r = self.client.post(
            reverse("hippie_website:network_query"),
            {
                "proteins": "FAKEPROT999",
                "output_type": "browser_vis",
                "direction": "none",
                "effect": "none",
                "negatome_edges": "none",
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
                "negatome_edges": "none",
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
        call_command("recompute_interaction_flags", stdout=StringIO())

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
            {"include_isoforms": "1"},
        )
        ids = [row["id"] for row in r.json()["interactions"]]
        self.assertIn(self.iso_ix.pk, ids)


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
        call_command("recompute_interaction_flags", stdout=StringIO())

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

    def test_score_range_filter(self):
        # The single fixture interaction scores 0.85.
        self.assertEqual(self._get(min_score=0.9)["total"], 0)
        self.assertEqual(self._get(min_score=0.5, max_score=0.9)["total"], 1)

    def test_source_filter(self):
        self.assertEqual(self._get(source=self.src.pk)["total"], 1)

    def test_experiment_filter(self):
        self.assertEqual(self._get(experiment=self.exp.pk)["total"], 1)

    def test_evidence_counts_denormalised(self):
        row = self._get()["interactions"][0]
        self.assertEqual(row["source_count"], 1)
        self.assertEqual(row["experiment_count"], 1)

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
        data = self._get(show="both", sort="symbol_a", dir="asc")
        self.assertEqual(data["total"], 2)
        symbols = [r["protein_a"]["symbol"] for r in data["interactions"]]
        self.assertEqual(symbols, sorted(symbols))


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
    def _post(self, **form):
        base = {
            "output_type": "browser_vis",
            "direction": "none",
            "effect": "none",
            "negatome_edges": "none",
            "score_min": "0.0",
        }
        base.update(form)
        return self.client.post(reverse("hippie_website:network_query"), base)

    def test_score_min_above_threshold_returns_no_edges(self):
        # Interaction has score=0.85; min=0.99 should exclude it
        r = self._post(proteins="BRCA1\nTP53", layer_0="on", score_min="0.99")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["network_result"]["edge_count"], 0)

    def test_layer_0_excludes_edges_outside_seed_set(self):
        # Add TP53–EGFR; seeds are only BRCA1+TP53 so TP53–EGFR should not appear
        make_interaction(self.tp53, self.egfr, score=0.7)
        r = self._post(proteins="BRCA1\nTP53", layer_0="on")
        result = r.context["network_result"]
        self.assertEqual(result["edge_count"], 1)

    def test_layer_1_includes_first_shell_partners(self):
        # layer_1 expands to first-shell; BRCA1 seed → TP53 partner included
        r = self._post(proteins="BRCA1", layer_1="on")
        result = r.context["network_result"]
        protein_names = {e["protein_a"] for e in result["interactions"]} | {
            e["protein_b"] for e in result["interactions"]
        }
        self.assertIn("TP53", protein_names)


# ---------------------------------------------------------------------------
# 21. BaitPreyAssociation model
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


class UpdateTissueDataCommandTest(TestCase):
    def test_required_args_are_enforced(self):
        with self.assertRaises(CommandError):
            call_command("update_tissue_data")

    def test_existing_gene_tissue_median_is_updated(self):
        gene = Gene.objects.create(entrez_id=101, entrez_name="GENE1")
        tissue = Tissue.objects.create(name="Liver")
        gene_tissue = GeneTissue.objects.create(
            gene=gene, tissue=tissue, median_rpkm=2.0
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
        self.assertEqual(gene_tissue.median_rpkm, 5.0)


# ---------------------------------------------------------------------------
# Source homepage URLs + per-pair evidence links
# ---------------------------------------------------------------------------


class SourceLinkTest(HippieTestCase):
    def test_helpers_resolve_case_insensitively(self):
        from .source_links import homepage_url, pair_search_url

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
        from .management.commands.hippie_update import _assign_source_urls

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
        from .management.commands.hippie_update import _parse_tech

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
        from .management.commands.hippie_update import _parse_tech

        result, skip = _parse_tech('psi-mi:"MI:0018"(two hybrid)')
        self.assertFalse(skip)
        assert result is not None
        _, name = result
        self.assertEqual(name, "Two-hybrid")  # normalized via _TECH_NORM

    def test_parse_interaction_type_handles_embedded_parens(self):
        from .management.commands.hippie_update import _parse_interaction_type

        itype = _parse_interaction_type(
            'psi-mi:"MI:0095"("proteinchip(r) on a surface-enhanced '
            'laser desorption/ionization")'
        )
        self.assertEqual(
            itype, "proteinchip(r) on a surface-enhanced laser desorption/ionization"
        )

    def test_parse_source_handles_embedded_parens(self):
        from .management.commands.hippie_update import _parse_source

        source = _parse_source(
            'psi-mi:"MI:0095"("proteinchip(r) on a surface-enhanced '
            'laser desorption/ionization")'
        )
        self.assertEqual(
            source, "proteinchip(r) on a surface-enhanced laser desorption/ionization"
        )


# ---------------------------------------------------------------------------
# ML Splits — orphan-aware stats, filter-aware medians, accession-based CSVs
# ---------------------------------------------------------------------------


class MLSplitStatsTest(TestCase):
    """Fix 1: a protein whose edges are ALL removed by the interaction-level
    filter (but which passes the protein-level filter) is an orphan — dropped
    from ``n_proteins`` and the medians, counted in ``n_orphaned_by_filter``.
    ``median_degree`` / ``median_avg_score`` reflect only surviving edges."""

    @classmethod
    def setUpTestData(cls):
        cls.a = make_protein("A", accession="ACC_A")
        cls.b = make_protein("B", accession="ACC_B")
        cls.c = make_protein("C", accession="ACC_C")
        cls.d = make_protein("D", accession="ACC_D")
        cls.e = make_protein("E", accession="ACC_E")
        # Survive min_score=0.5:
        make_interaction(cls.a, cls.b, score=0.85)
        make_interaction(cls.a, cls.c, score=0.85)
        # Filtered out at min_score=0.5:
        make_interaction(cls.b, cls.c, score=0.15)  # B, C keep a surviving edge via A
        make_interaction(cls.a, cls.d, score=0.15)  # raises A's *global* degree only
        make_interaction(cls.d, cls.e, score=0.15)  # D and E become filter-orphans
        call_command("recompute_protein_stats", stdout=StringIO())

    def _stats(self, **overrides):
        from .services.generate_splits import SplitParams, build_interaction_queryset
        from .views import _interaction_stats, _protein_stats

        params = SplitParams(**overrides)
        interaction, degree_by_node, score_sum_by_node = _interaction_stats(
            build_interaction_queryset(params)
        )
        protein = _protein_stats(params, degree_by_node, score_sum_by_node)
        return interaction, protein

    def test_orphans_excluded_and_medians_are_filter_aware(self):
        interaction, protein = self._stats(min_score=0.5)

        # Only A–B and A–C survive.
        self.assertEqual(interaction["n_interactions"], 2)

        # A, B, C survive; D and E pass the (empty) protein filter but have no
        # surviving edge → orphaned, so excluded from n_proteins.
        self.assertEqual(protein["n_proteins"], 3)
        self.assertEqual(protein["n_orphaned_by_filter"], 2)

        # Filtered degrees A:2, B:1, C:1 → median 1 (global 3,2,2 would give 2).
        self.assertEqual(protein["median_degree"], 1)
        # Filtered avg is 0.85 for every survivor; the global avg (mixing the
        # 0.15 edges) would be lower — proving the median is filter-aware.
        self.assertEqual(protein["median_avg_score"], 0.85)

    def test_interaction_histogram_and_median_from_group_by(self):
        # Locks the DB GROUP-BY rework of _interaction_stats against the old
        # per-edge Python scan, on the unfiltered fixture (all 5 edges).
        interaction, _ = self._stats()
        self.assertEqual(interaction["n_interactions"], 5)
        # scores: 0.85, 0.85, 0.15, 0.15, 0.15 → median lands in the [0.1, 0.2) bin.
        self.assertTrue(0.1 <= interaction["median_score"] < 0.2)
        hist = {b["label"]: b["count"] for b in interaction["score_histogram"]}
        self.assertEqual(hist["0.1"], 3)
        self.assertEqual(hist["0.8"], 2)

    def test_self_loop_counts_toward_degree_twice(self):
        # A self-loop (protein_1 == protein_2) lands in both GROUP-BY sides,
        # reproducing the old loop that incremented both endpoints.
        from .services.generate_splits import SplitParams, build_interaction_queryset
        from .views import _interaction_stats

        f = make_protein("F", accession="ACC_F")
        make_interaction(f, f, score=0.9)  # self-loop, survives the filter
        _, degree_by_node, _sum = _interaction_stats(
            build_interaction_queryset(SplitParams(min_score=0.5))
        )
        self.assertEqual(degree_by_node[f.pk], 2)

    def test_stats_endpoint_wires_through(self):
        resp = self.client.post(
            reverse("hippie_website:browse_splits_stats"),
            data=json.dumps({"min_score": 0.5}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(payload["protein"]["n_proteins"], 3)
        self.assertEqual(payload["protein"]["n_orphaned_by_filter"], 2)
        self.assertEqual(payload["protein"]["median_avg_score"], 0.85)
        self.assertEqual(payload["interaction"]["n_interactions"], 2)


class MLSplitPruneUnitTest(SimpleTestCase):
    """Fix 2 mechanism: ``drop_exclusive_nodes`` removes a split's zero-degree
    nodes, so the post-prune per-split node-count sum diverges from the
    pre-partition node count — exactly what ``SplitSummary.n_proteins`` now
    sums post-prune instead of reading pre-prune."""

    def test_prune_drops_orphan_and_diverges_from_pre_prune_count(self):
        import networkx as nx

        from .services.generate_splits import EdgePartition, SplitParams

        p = EdgePartition.__new__(EdgePartition)  # skip __init__ (no DB access)
        p.params = SplitParams(seed=1)
        p.interaction_graph = nx.Graph()
        p.interaction_graph.add_edges_from([(1, 2), (3, 4)])
        p.interaction_graph.add_node(5)  # orphan present pre-partition
        p.discarded_nodes = set()

        pos_G = nx.Graph()
        pos_G.add_edges_from([(1, 2), (3, 4)])
        pos_G.add_node(5)  # zero-degree in this split's positive graph
        neg_G = nx.Graph()
        neg_G.add_edges_from([(1, 3), (2, 4)])
        p.selected_sets = [(pos_G, neg_G)]

        p.drop_exclusive_nodes()

        self.assertEqual(p.discarded_nodes, {5})
        pre_prune = p.interaction_graph.number_of_nodes()  # 5
        post_prune = sum(pos.number_of_nodes() for pos, _ in p.selected_sets)  # 4
        self.assertNotEqual(post_prune, pre_prune)


class MLSplitNegativeSamplerTest(SimpleTestCase):
    """Perf rewrite: the degree-weighted sampler must still return exactly
    ``n_edges`` valid negatives — no self-loops, duplicates, or real positives."""

    def test_sampler_returns_valid_balanced_negatives(self):
        import networkx as nx
        import numpy as np

        from .services.generate_splits import EdgePartition, SplitParams

        np.random.seed(0)  # the sampler draws from np.random directly
        ep = EdgePartition.__new__(EdgePartition)
        ep.params = SplitParams(seed=1)
        # Sparse path graph: 6 nodes, 5 edges → 10 non-edges to sample from.
        ep.interaction_graph = nx.Graph([(1, 2), (2, 3), (3, 4), (4, 5), (5, 6)])
        node_set = set(ep.interaction_graph.nodes())

        pos_copy, neg = ep.get_random_balanced_negative_complement(node_set)

        positives = {tuple(sorted(e)) for e in ep.interaction_graph.edges()}
        neg_edges = {tuple(sorted(e)) for e in neg.edges()}
        self.assertEqual(neg.number_of_edges(), ep.interaction_graph.number_of_edges())
        self.assertTrue(neg_edges.isdisjoint(positives))  # never reuses a positive
        self.assertFalse(any(u == v for u, v in neg_edges))  # no self-loops
        # pos_copy is a mutable copy of the positive subgraph.
        self.assertEqual(
            pos_copy.number_of_edges(), ep.interaction_graph.number_of_edges()
        )


class MLSplitGenerateTest(TestCase):
    """Fix 2 + Fix 3 end-to-end: run a real split job on a small sparse graph
    and check the summary count is post-prune-consistent and the CSVs use
    UniProt accessions (never internal PKs)."""

    @classmethod
    def setUpTestData(cls):
        # 24 proteins wired as a cycle + a chord ring: connected but sparse
        # (avg degree ~4), so every balanced partition keeps internal edges AND
        # leaves non-edges for negative sampling.
        cls.proteins = [
            make_protein(f"P{i}", accession=f"ACC{i:03d}") for i in range(24)
        ]
        seen: set[tuple[int, int]] = set()
        for i in range(24):
            for j in ((i + 1) % 24, (i + 5) % 24):
                a, b = sorted((i, j))
                if a != b and (a, b) not in seen:
                    seen.add((a, b))
                    make_interaction(cls.proteins[a], cls.proteins[b], score=0.8)
        cls.accessions = {p.uniprot_accession for p in cls.proteins}

    def test_generate_writes_accession_csvs_and_consistent_summary(self):
        import numpy as np

        from .services.generate_splits import (
            SplitParams,
            generate_splits,
            get_interaction_graph,
        )

        np.random.seed(0)  # negative sampling draws from np.random
        params = SplitParams(seed=1)
        with tempfile.TemporaryDirectory() as td:
            work_dir = Path(td)
            summary = generate_splits(params, work_dir, lambda step, frac: None)

            # Fix 2: top-level count == sum of per-split (post-prune) counts.
            self.assertEqual(
                summary.n_proteins, sum(s["n_proteins"] for s in summary.splits)
            )
            if summary.n_discarded_nodes > 0:
                pre_prune = get_interaction_graph(params).number_of_nodes()
                self.assertLess(summary.n_proteins, pre_prune)

            for name in ("train", "validation", "test"):
                pos = (work_dir / f"{name}_pos.csv").read_text().splitlines()
                neg = (work_dir / f"{name}_neg.csv").read_text().splitlines()
                self.assertEqual(
                    pos[0], "protein_1_accession,protein_2_accession,score"
                )
                self.assertEqual(neg[0], "protein_1_accession,protein_2_accession")
                # Fix 3: every endpoint is a known accession string, never a PK.
                for row in pos[1:]:
                    a, b, _score = row.split(",")
                    self.assertIn(a, self.accessions)
                    self.assertIn(b, self.accessions)
                for row in neg[1:]:
                    a, b = row.split(",")
                    self.assertIn(a, self.accessions)
                    self.assertIn(b, self.accessions)

            # Sampler balances each split: n_neg == n_pos.
            for s in summary.splits:
                self.assertEqual(s["n_neg"], s["n_pos"])

    def test_isoform_accession_resolves_via_inherited_field(self):
        # Fix 3 for isoforms: MTI shares the pk, so the accession lookup returns
        # the isoform-specific "-2" accession, not the canonical parent's.
        from .models import Protein

        canonical = self.proteins[0]
        iso = Isoform.objects.create(
            gene=canonical.gene,
            uniprot_accession="ACC000-2",
            general_protein=canonical,
        )
        mapping = dict(
            Protein.objects.filter(pk__in=[iso.pk]).values_list(
                "pk", "uniprot_accession"
            )
        )
        self.assertEqual(mapping[iso.pk], "ACC000-2")


# ---------------------------------------------------------------------------
# Batch 3 — shared full-parity filters on the two query APIs
# ---------------------------------------------------------------------------


class Batch3ProteinQueryFilterTest(HippieTestCase):
    """protein_query_api now honours the full shared filter set; protein-level
    filters (score/source/experiment/swissprot) apply to the partner (B) side."""

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

    def test_swissprot_trembl_excludes_all_partners(self):
        # Every fixture protein defaults is_swissprot=True → TrEMBL → empty.
        self.assertEqual(
            len(self._query("BRCA1", swissprot="trembl")["interactions"]), 0
        )
        self.assertGreaterEqual(
            len(self._query("BRCA1", swissprot="swissprot")["interactions"]), 1
        )


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
            include_isoforms=True,
        )
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["is_noninteraction"])
        self.assertGreaterEqual(results[0]["score"], 0)


class Batch3FilterMetaInteractionTypesTest(HippieTestCase):
    def test_interaction_types_returned(self):
        from .models import InteractionType

        InteractionType.objects.create(name="direct interaction", psi_mi_code="MI:0407")
        r = self.client.get(reverse("hippie_website:browse_filter_meta"))
        data = json.loads(r.content)
        self.assertIn("interaction_types", data)
        names = [x["name"] for x in data["interaction_types"]]
        self.assertIn("direct interaction", names)
