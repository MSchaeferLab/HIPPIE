import json
import tempfile
from pathlib import Path

from django.test import TestCase, SimpleTestCase
from django.urls import reverse

from ..models import (
    Isoform,
    Protein,
    Tissue,
    GeneTissue,
)
from .factories import (
    make_protein,
    make_interaction,
    recompute_stats,
    recompute_flags,
)


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
        recompute_stats()

    def _stats(self, **overrides):
        from ..services.generate_splits import SplitParams, build_interaction_queryset
        from ..views import _interaction_stats, _protein_stats

        params = SplitParams(**overrides)
        iqs = build_interaction_queryset(params)
        interaction, degree_by_node, score_sum_by_node = _interaction_stats(iqs)
        protein = _protein_stats(params, degree_by_node, score_sum_by_node, iqs)
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
        from ..services.generate_splits import SplitParams, build_interaction_queryset
        from ..views import _interaction_stats

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
        # Batch 6: the degree histogram now lives on the protein box, not the
        # interaction box.
        self.assertIn("degree_histogram", payload["protein"])
        self.assertNotIn("degree_histogram", payload["interaction"])
        # Filtered degrees are A:2, B:1, C:1 → bucket "1" holds B and C, "2"
        # holds A. Checks the histogram is built from degree_by_node (moved
        # with the relocation), not an empty/wrong dict.
        degree_hist = {
            b["label"]: b["count"] for b in payload["protein"]["degree_histogram"]
        }
        self.assertEqual(degree_hist["1"], 2)
        self.assertEqual(degree_hist["2"], 1)


class MLSplitIsoformModeTest(TestCase):
    """3-way ``isoform_mode`` gate on ``build_interaction_queryset`` and the
    protein-side stats (``_protein_filtered_qs`` / ``_protein_stats``)."""

    @classmethod
    def setUpTestData(cls):
        cls.a = make_protein("ISO_A", accession="ISOACC_A")
        cls.b = make_protein("ISO_B", accession="ISOACC_B")
        cls.iso_a = Isoform.objects.create(
            gene=cls.a.gene,
            uniprot_name="",
            uniprot_accession="ISOACC_A-2",
            general_protein=cls.a,
        )
        cls.canonical_ix = make_interaction(cls.a, cls.b, score=0.9)
        cls.isoform_ix = make_interaction(cls.iso_a, cls.b, score=0.8)
        recompute_stats()
        recompute_flags()

    def _iqs_pks(self, isoform_mode):
        from ..services.generate_splits import SplitParams, build_interaction_queryset

        qs = build_interaction_queryset(SplitParams(isoform_mode=isoform_mode))
        return set(qs.values_list("pk", flat=True))

    def test_general_excludes_isoform_edges(self):
        pks = self._iqs_pks("general")
        self.assertIn(self.canonical_ix.pk, pks)
        self.assertNotIn(self.isoform_ix.pk, pks)

    def test_isoforms_mode_keeps_only_isoform_edges(self):
        pks = self._iqs_pks("isoforms")
        self.assertNotIn(self.canonical_ix.pk, pks)
        self.assertIn(self.isoform_ix.pk, pks)

    def test_both_mode_keeps_every_edge(self):
        pks = self._iqs_pks("both")
        self.assertIn(self.canonical_ix.pk, pks)
        self.assertIn(self.isoform_ix.pk, pks)

    def test_protein_stats_n_isoforms_only_counted_outside_general(self):
        from ..services.generate_splits import SplitParams, build_interaction_queryset
        from ..views import _interaction_stats, _protein_stats

        for mode, expect_isoforms in (
            ("general", 0),
            ("isoforms", 1),
            ("both", 1),
        ):
            params = SplitParams(isoform_mode=mode)
            iqs = build_interaction_queryset(params)
            _interaction, degree_by_node, score_sum_by_node = _interaction_stats(iqs)
            protein = _protein_stats(params, degree_by_node, score_sum_by_node, iqs)
            self.assertEqual(protein["n_isoforms"], expect_isoforms, mode)


class MLSplitPruneUnitTest(SimpleTestCase):
    """Fix 2 mechanism: ``drop_exclusive_nodes`` removes a split's zero-degree
    nodes, so the post-prune per-split node-count sum diverges from the
    pre-partition node count — exactly what ``SplitSummary.n_proteins`` now
    sums post-prune instead of reading pre-prune."""

    def test_prune_drops_orphan_and_diverges_from_pre_prune_count(self):
        import networkx as nx

        from ..services.generate_splits import EdgePartition, SplitParams

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

        from ..services.generate_splits import EdgePartition, SplitParams

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

        from ..services.generate_splits import (
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
            # Individual split sizes: catches a swapped/zeroed split, which the
            # sum-only check above can't (e.g. a 24-0-0 split sums the same as
            # 12-6-6). Values are deterministic for this fixture + seed=1
            # (verified stable across repeated runs).
            by_name = {s["name"]: s for s in summary.splits}
            self.assertEqual(by_name["train"]["n_proteins"], 12)
            self.assertEqual(by_name["validation"]["n_proteins"], 6)
            self.assertEqual(by_name["test"]["n_proteins"], 6)
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


class MLSplitFilteredOutTest(TestCase):
    """Task 10: ``n_filtered_out`` counts proteins removed by the protein-level
    filter (here a tissue filter) relative to the full protein table — distinct
    from ``n_orphaned_by_filter`` (proteins that pass the filter but lose all
    edges). ``tissue_coverage`` was removed from the stats payload."""

    @classmethod
    def setUpTestData(cls):
        # Distinct genes per protein so the tissue filter is per-protein.
        cls.a = make_protein("A", gene_id=101, accession="ACC_A")
        cls.b = make_protein("B", gene_id=102, accession="ACC_B")
        cls.c = make_protein("C", gene_id=103, accession="ACC_C")
        cls.d = make_protein("D", gene_id=104, accession="ACC_D")

        make_interaction(cls.a, cls.b, score=0.85)  # survives (both in Blood)
        make_interaction(cls.c, cls.d, score=0.85)  # C, D removed by tissue filter
        recompute_stats()

        cls.blood = Tissue.objects.create(name="Blood")
        # Only A and B are expressed in Blood → C and D are filtered out.
        GeneTissue.objects.create(gene=cls.a.gene, tissue=cls.blood, median_rpkm=5.0)
        GeneTissue.objects.create(gene=cls.b.gene, tissue=cls.blood, median_rpkm=5.0)

    def test_filtered_out_counts_protein_level_removals(self):
        from ..services.generate_splits import SplitParams, build_interaction_queryset
        from ..views import _interaction_stats, _protein_stats

        params = SplitParams(min_score=0.5, tissue_ids=(self.blood.pk,))
        iqs = build_interaction_queryset(params)
        _interaction, degree_by_node, score_sum = _interaction_stats(iqs)
        protein = _protein_stats(params, degree_by_node, score_sum, iqs)

        # Full table = 4 proteins; only A, B pass the Blood tissue filter.
        self.assertEqual(protein["n_filtered_out"], 2)  # C, D removed by filter
        self.assertEqual(protein["n_proteins"], 2)  # A, B survive with an edge
        self.assertEqual(protein["n_orphaned_by_filter"], 0)  # no filter-orphans
        # tissue_coverage was removed from the payload.
        self.assertNotIn("tissue_coverage", protein)


class MLSplitQueuePositionTest(TestCase):
    """Batch 6: the status endpoint reports queue_position = number of PENDING
    jobs created before this one (FIFO). RUNNING/DONE jobs never count, and a
    picked-up (non-PENDING) job reports 0."""

    def test_queue_position_counts_earlier_pending_only(self):
        from datetime import timedelta

        from django.utils import timezone

        from ..models import SplitJob

        base = timezone.now()
        j_old = SplitJob.objects.create(params={}, status="PENDING")
        j_run = SplitJob.objects.create(params={}, status="RUNNING")
        j_new = SplitJob.objects.create(params={}, status="PENDING")
        # created_at is auto_now_add → force deterministic ordering via update().
        SplitJob.objects.filter(pk=j_old.pk).update(created_at=base)
        SplitJob.objects.filter(pk=j_run.pk).update(
            created_at=base + timedelta(seconds=1)
        )
        SplitJob.objects.filter(pk=j_new.pk).update(
            created_at=base + timedelta(seconds=2)
        )

        def qpos(job):
            resp = self.client.get(
                reverse("hippie_website:browse_splits_status", args=[job.pk])
            )
            self.assertEqual(resp.status_code, 200)
            return resp.json()["queue_position"]

        # j_new: one earlier PENDING (j_old); the earlier RUNNING job is excluded.
        self.assertEqual(qpos(j_new), 1)
        # j_old: nothing precedes it.
        self.assertEqual(qpos(j_old), 0)
        # A RUNNING job always reports 0 regardless of predecessors.
        self.assertEqual(qpos(j_run), 0)

    def test_queue_position_counts_all_earlier_pending_not_just_presence(self):
        # Regression guard: a boolean "anything pending ahead" check would
        # also report 1 here, indistinguishable from the real FIFO count.
        from datetime import timedelta

        from django.utils import timezone

        from ..models import SplitJob

        base = timezone.now()
        jobs = [SplitJob.objects.create(params={}, status="PENDING") for _ in range(4)]
        for i, job in enumerate(jobs):
            SplitJob.objects.filter(pk=job.pk).update(
                created_at=base + timedelta(seconds=i)
            )

        resp = self.client.get(
            reverse("hippie_website:browse_splits_status", args=[jobs[-1].pk])
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["queue_position"], 3)

    def test_queue_position_tiebreaks_identical_created_at(self):
        # Two PENDING jobs sharing the same created_at (e.g. same-millisecond
        # creation) must still be given a strict, deterministic order rather
        # than both counting (or neither counting) the other.
        from django.utils import timezone

        from ..models import SplitJob

        now = timezone.now()
        j1 = SplitJob.objects.create(params={}, status="PENDING")
        j2 = SplitJob.objects.create(params={}, status="PENDING")
        SplitJob.objects.filter(pk__in=[j1.pk, j2.pk]).update(created_at=now)

        earlier, later = sorted([j1, j2], key=lambda j: j.pk)

        resp = self.client.get(
            reverse("hippie_website:browse_splits_status", args=[later.pk])
        )
        self.assertEqual(resp.json()["queue_position"], 1)
        resp = self.client.get(
            reverse("hippie_website:browse_splits_status", args=[earlier.pk])
        )
        self.assertEqual(resp.json()["queue_position"], 0)


class MLSplitEndpointNotFoundTest(TestCase):
    """browse_splits_status/download/create for a nonexistent or not-yet-done
    job id must 404, not 500 or silently succeed."""

    def test_status_404_for_unknown_job_id(self):
        import uuid

        resp = self.client.get(
            reverse("hippie_website:browse_splits_status", args=[uuid.uuid4()])
        )
        self.assertEqual(resp.status_code, 404)

    def test_download_404_for_unknown_job_id(self):
        import uuid

        resp = self.client.get(
            reverse("hippie_website:browse_splits_download", args=[uuid.uuid4()])
        )
        self.assertEqual(resp.status_code, 404)

    def test_download_404_for_job_not_yet_done(self):
        from ..models import SplitJob

        job = SplitJob.objects.create(params={}, status="PENDING")
        resp = self.client.get(
            reverse("hippie_website:browse_splits_download", args=[job.pk])
        )
        self.assertEqual(resp.status_code, 404)

    def test_create_enqueues_job_and_returns_202(self):
        from unittest.mock import patch

        from ..models import SplitJob

        with patch("hippie_website.views.run_split_job.delay") as mock_delay:
            resp = self.client.post(
                reverse("hippie_website:browse_splits_create"),
                data=json.dumps({}),
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 202)
        payload = resp.json()
        self.assertEqual(payload["status"], "PENDING")
        job = SplitJob.objects.get(pk=payload["job_id"])
        mock_delay.assert_called_once_with(str(job.id))

    def test_create_400_for_invalid_params(self):
        resp = self.client.post(
            reverse("hippie_website:browse_splits_create"),
            data=json.dumps({"min_score": 2.0}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)


class PartitionDisjointTest(SimpleTestCase):
    """Moved out of services.generate_splits (where it was an uncollected
    module-level test function): the Kernighan–Lin partition must produce three
    pairwise-disjoint node sets."""

    def test_partition_respects_disjoint_node_sets(self):
        import networkx as nx

        from ..services.generate_splits import EdgePartition, SplitParams

        params = SplitParams(seed=1)
        p = EdgePartition.__new__(EdgePartition)  # skip __init__ (no DB needed)
        p.params = params
        p.interaction_graph = nx.barbell_graph(10, 0)  # two cliques + a bridge
        p.node_sets = []
        p.selected_sets = []
        p.discarded_edges = []
        p.discarded_nodes = set()

        p.generate_splits()
        a, b, c = p.node_sets
        self.assertTrue(a.isdisjoint(b))
        self.assertTrue(b.isdisjoint(c))
        self.assertTrue(a.isdisjoint(c))
