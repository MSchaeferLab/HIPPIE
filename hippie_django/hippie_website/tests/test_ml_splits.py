import json
import os
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


class MLSplitExportTest(TestCase):
    """The ppis.csv the pipeline actually reads.

    This is the seam worth testing hardest: an accession the precomputed
    artifacts do not cover does not crash the pipeline, it produces a protein
    the KaHIP partition has no cluster for. The failure is silent, so the drop
    count has to be computed and asserted rather than assumed to be zero.
    """

    @classmethod
    def setUpTestData(cls):
        # 24 proteins wired as a cycle + a chord ring: connected but sparse
        # (avg degree ~4).
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

    def test_writes_header_and_every_filtered_edge(self):
        from ..services.export_ppis import write_ppis_csv
        from ..services.generate_splits import SplitParams, build_interaction_queryset

        params = SplitParams()
        expected = build_interaction_queryset(params).count()

        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "ppis.csv"
            result = write_ppis_csv(params, dest, known_accessions=self.accessions)

            lines = dest.read_text().splitlines()
            # Column names and order match HIPPIE-current.csv, the file the
            # pipeline was validated against.
            self.assertEqual(lines[0], "protein1,protein2,score")
            self.assertEqual(len(lines) - 1, expected)
            self.assertEqual(result.n_written, expected)
            self.assertEqual(result.n_dropped, 0)
            for row in lines[1:]:
                a, b, _score = row.split(",")
                self.assertIn(a, self.accessions)
                self.assertIn(b, self.accessions)

    def test_drops_and_counts_edges_the_artifacts_do_not_cover(self):
        from ..services.export_ppis import write_ppis_csv
        from ..services.generate_splits import SplitParams, build_interaction_queryset

        params = SplitParams()
        total = build_interaction_queryset(params).count()
        # Pretend the artifacts are missing one protein. Every edge touching it
        # must be dropped, and the drop must be *reported*, not swallowed.
        missing = self.proteins[0].uniprot_accession
        known = self.accessions - {missing}

        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "ppis.csv"
            result = write_ppis_csv(params, dest, known_accessions=known)

            self.assertGreater(result.n_dropped_unknown, 0)
            self.assertEqual(result.n_written + result.n_dropped_unknown, total)
            self.assertEqual(result.n_dropped_blank, 0)
            self.assertNotIn(missing, dest.read_text())

    def test_export_honours_the_interaction_filters(self):
        """The exporter and the stats box must describe the same dataset."""
        from ..services.export_ppis import write_ppis_csv
        from ..services.generate_splits import SplitParams, build_interaction_queryset

        make_interaction(self.proteins[2], self.proteins[9], score=0.1)
        params = SplitParams(min_score=0.5)
        expected = build_interaction_queryset(params).count()

        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "ppis.csv"
            result = write_ppis_csv(params, dest, known_accessions=self.accessions)
        self.assertEqual(result.n_written, expected)

    def test_samplesheet_names_every_split_only_input_with_absolute_paths(self):
        """--split_only makes all five precomputed columns mandatory (main.nf's
        buildDatasetsChannel), and Nextflow resolves relative samplesheet paths
        against its launch dir — which is the job dir, not the artifact dir."""
        import csv as _csv

        from ..services.export_ppis import ARTIFACT_FILES, write_samplesheet

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            artifacts = {}
            for key, name in ARTIFACT_FILES.items():
                path = base / name
                path.write_text("x")
                artifacts[key] = path
            ppis = base / "ppis.csv"
            ppis.write_text("protein1,protein2,score\n")

            dest = write_samplesheet(
                base / "samplesheet.csv", "hippie", ppis, artifacts
            )
            rows = list(_csv.DictReader(dest.open()))

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], "hippie")
        for key in ("ppis", *ARTIFACT_FILES):
            self.assertTrue(row[key].startswith("/"), f"{key} is not absolute")

    def test_missing_artifact_is_reported_before_nextflow_starts(self):
        from ..services.export_ppis import ARTIFACT_FILES, artifact_paths

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            for name in list(ARTIFACT_FILES.values())[:-1]:
                (base / name).write_text("x")
            absent = list(ARTIFACT_FILES.values())[-1]
            with self.assertRaises(FileNotFoundError) as ctx:
                artifact_paths(base)
        self.assertIn(absent, str(ctx.exception))

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


class MLSplitCandidateNegativesTest(TestCase):
    """build_noninteraction_queryset / write_candidate_negatives_csv — the
    "Download raw data" package's candidate_network input (see
    services/raw_export.py). A NonInteraction has no source/experiment/type
    evidence, so a filter along those lines must disable the whole leg rather
    than silently return nothing meaningful."""

    @classmethod
    def setUpTestData(cls):
        from .factories import make_noninteraction

        cls.proteins = [
            make_protein(f"N{i}", accession=f"NEG{i:03d}") for i in range(4)
        ]
        cls.ni_low = make_noninteraction(cls.proteins[0], cls.proteins[1], score=0.2)
        cls.ni_high = make_noninteraction(cls.proteins[2], cls.proteins[3], score=0.9)

    def test_none_when_a_source_like_filter_is_active(self):
        from ..services.generate_splits import (
            SplitParams,
            build_noninteraction_queryset,
        )

        self.assertIsNone(build_noninteraction_queryset(SplitParams(source_ids=(1,))))
        self.assertIsNone(
            build_noninteraction_queryset(SplitParams(experiment_ids=(1,)))
        )
        self.assertIsNone(build_noninteraction_queryset(SplitParams(type_ids=(1,))))

    def test_score_bounds_apply_like_the_positives(self):
        from ..services.generate_splits import (
            SplitParams,
            build_noninteraction_queryset,
        )

        qs = build_noninteraction_queryset(SplitParams(min_score=0.5))
        self.assertEqual(list(qs.values_list("pk", flat=True)), [self.ni_high.pk])

    def test_write_candidate_negatives_csv_uses_score_as_w(self):
        from ..services.export_ppis import write_candidate_negatives_csv
        from ..services.generate_splits import SplitParams

        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "candidate_negatives.csv"
            result = write_candidate_negatives_csv(SplitParams(max_score=0.5), dest)

            self.assertEqual(result.n_written, 1)
            lines = dest.read_text().splitlines()
            self.assertEqual(lines[0], "protein1,protein2,w")
            a, b, w = lines[1].split(",")
            self.assertEqual(float(w), 0.2)
            self.assertIn(
                a,
                {
                    self.proteins[0].uniprot_accession,
                    self.proteins[1].uniprot_accession,
                },
            )
            self.assertIn(
                b,
                {
                    self.proteins[0].uniprot_accession,
                    self.proteins[1].uniprot_accession,
                },
            )

    def test_write_candidate_negatives_csv_writes_nothing_when_none_match(self):
        from ..services.export_ppis import write_candidate_negatives_csv
        from ..services.generate_splits import SplitParams

        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "candidate_negatives.csv"
            result = write_candidate_negatives_csv(SplitParams(min_score=0.99), dest)
            self.assertEqual(result.n_written, 0)
            self.assertFalse(dest.exists())

    def test_write_candidate_negatives_csv_writes_nothing_when_source_filter_active(
        self,
    ):
        from ..services.export_ppis import write_candidate_negatives_csv
        from ..services.generate_splits import SplitParams

        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "candidate_negatives.csv"
            result = write_candidate_negatives_csv(SplitParams(source_ids=(1,)), dest)
            self.assertEqual(result.n_written, 0)
            self.assertFalse(dest.exists())


class MLSplitRawDataViewTest(TestCase):
    """browse_splits_raw_data: the "Download raw data" fast path — synchronous,
    no Celery, packages the pipeline's *standard*-mode inputs rather than the
    server's own --split_only shortcut."""

    @classmethod
    def setUpTestData(cls):
        cls.proteins = [
            make_protein(f"R{i}", accession=f"RAW{i:03d}") for i in range(6)
        ]
        for i in range(5):
            make_interaction(cls.proteins[i], cls.proteins[i + 1], score=0.8)

    def _post(self, payload):
        return self.client.post(
            reverse("hippie_website:browse_splits_raw_data"),
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_happy_path_without_curated_negatives(self):
        import csv as _csv
        import io
        import zipfile

        from ..models import SplitJob

        resp = self._post({})
        self.assertEqual(resp.status_code, 201)
        payload = resp.json()
        self.assertEqual(payload["status"], "DONE")

        job = SplitJob.objects.get(pk=payload["job_id"])
        self.assertEqual(job.job_type, "RAW_DATA")
        self.assertEqual(job.summary["negatives_exported"], 0)
        self.assertIn(
            "nextflow run ../ppi-splitting-pipeline/main.nf",
            job.summary["pipeline_command"],
        )
        self.assertTrue(os.path.isfile(job.zip_path))

        with zipfile.ZipFile(job.zip_path) as zf:
            names = zf.namelist()
            self.assertIn("ppis.csv", names)
            self.assertIn("samplesheet.csv", names)
            self.assertIn("README.txt", names)
            self.assertNotIn("candidate_negatives.csv", names)
            rows = list(_csv.DictReader(io.TextIOWrapper(zf.open("samplesheet.csv"))))
            self.assertEqual(rows[0]["candidate_network"], "")

    def test_happy_path_with_curated_negatives_wires_candidate_network(self):
        import csv as _csv
        import io
        import zipfile

        from ..models import SplitJob
        from .factories import make_noninteraction

        make_noninteraction(self.proteins[0], self.proteins[2], score=0.2)
        resp = self._post({})
        job = SplitJob.objects.get(pk=resp.json()["job_id"])
        self.assertEqual(job.summary["negatives_exported"], 1)

        with zipfile.ZipFile(job.zip_path) as zf:
            self.assertIn("candidate_negatives.csv", zf.namelist())
            rows = list(_csv.DictReader(io.TextIOWrapper(zf.open("samplesheet.csv"))))
            self.assertEqual(rows[0]["candidate_network"], "candidate_negatives.csv")

    def test_zero_interactions_fails_the_job_not_the_request(self):
        from ..models import SplitJob

        resp = self._post({"min_score": 0.99})
        self.assertEqual(resp.status_code, 201)
        job = SplitJob.objects.get(pk=resp.json()["job_id"])
        self.assertEqual(job.status, "FAILED")
        self.assertIn("no interactions", job.error)

    def test_never_touches_the_celery_queue(self):
        from unittest.mock import patch

        with patch("hippie_website.views.run_split_job.apply_async") as mock_send:
            resp = self._post({})
        mock_send.assert_not_called()
        self.assertEqual(resp.status_code, 201)

    def test_job_type_round_trips_through_status_endpoint(self):
        job_id = self._post({}).json()["job_id"]
        status = self.client.get(
            reverse("hippie_website:browse_splits_status", args=[job_id])
        ).json()
        self.assertEqual(status["job_type"], "RAW_DATA")


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
        GeneTissue.objects.create(gene=cls.a.gene, tissue=cls.blood, median_tpm=5.0)
        GeneTissue.objects.create(gene=cls.b.gene, tissue=cls.blood, median_tpm=5.0)

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

        with patch("hippie_website.views.run_split_job.apply_async") as mock_send:
            resp = self.client.post(
                reverse("hippie_website:browse_splits_create"),
                data=json.dumps({}),
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 202)
        payload = resp.json()
        self.assertEqual(payload["status"], "PENDING")
        job = SplitJob.objects.get(pk=payload["job_id"])
        # task_id == job id on purpose: browse_splits_cancel revokes a queued
        # message by job id, so a generated Celery id would make cancel a no-op.
        mock_send.assert_called_once_with(args=[str(job.id)], task_id=str(job.id))

    def test_create_ignores_retired_sampling_keys(self):
        """neg_ratio / seed are no longer part of the contract: the pipeline
        fixes both. Sending them must not 400 (old bookmarks still carry them)
        and must not be stored as if they had taken effect."""
        from unittest.mock import patch

        from ..models import SplitJob

        with patch("hippie_website.views.run_split_job.apply_async"):
            resp = self.client.post(
                reverse("hippie_website:browse_splits_create"),
                data=json.dumps({"neg_ratio": 99, "seed": 7}),
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 202)
        job = SplitJob.objects.get(pk=resp.json()["job_id"])
        self.assertNotIn("neg_ratio", job.params)
        self.assertNotIn("seed", job.params)

    def test_create_400_for_invalid_params(self):
        resp = self.client.post(
            reverse("hippie_website:browse_splits_create"),
            data=json.dumps({"min_score": 2.0}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        # The documented contract is "400 with the failing constraint in the
        # message". A raised BadRequest would render Django's generic, empty 400
        # page here with DEBUG=False, so assert on the body, not just the status.
        self.assertEqual(resp["Content-Type"], "application/json")
        self.assertIn("min_score", resp.json()["error"])

    def test_stats_400_names_the_failing_constraint(self):
        resp = self.client.post(
            reverse("hippie_website:browse_splits_stats"),
            data=json.dumps({"min_avg_score": 99}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("min_avg_score", resp.json()["error"])


class MinDegreeGlobalTest(TestCase):
    """The degree gate is global; the reported median degree is not.

    A reviewer set source=I2D with a minimum degree of 5 and saw a median degree
    of 1, and read that as a broken filter. It is not: ``min_degree_global``
    gates ``Protein.degree`` — the count across all of HIPPIE — while
    ``median_degree`` counts only the edges left after the source filter. The two
    numbers answer different questions and legitimately diverge. These tests pin
    that behaviour so it cannot drift silently, since the UI labels now promise
    exactly this.
    """

    @classmethod
    def setUpTestData(cls):
        from ..models import Source

        cls.hub = make_protein("HUB", accession="ACC_HUB")
        cls.partners = [make_protein(f"P{i}", accession=f"ACC_P{i}") for i in range(6)]
        # HUB is well connected across the database as a whole…
        for p in cls.partners:
            make_interaction(cls.hub, p, score=0.9)
        # …but only one of those edges is reported by the narrow source.
        cls.narrow = Source.objects.create(name="I2D", n_connected_interactions=1)
        cls.wide = Source.objects.create(name="BioGRID", n_connected_interactions=5)
        edges = list(cls.hub.interactions_as_1.all()) + list(
            cls.hub.interactions_as_2.all()
        )
        for i, edge in enumerate(edges):
            edge.sources.add(cls.narrow if i == 0 else cls.wide)
        recompute_stats()

    def _stats(self, **overrides):
        from ..services.generate_splits import SplitParams, build_interaction_queryset
        from ..views import _interaction_stats, _protein_stats

        params = SplitParams(**overrides)
        iqs = build_interaction_queryset(params)
        interaction, degree_by_node, score_sum_by_node = _interaction_stats(iqs)
        return interaction, _protein_stats(
            params, degree_by_node, score_sum_by_node, iqs
        )

    def test_gate_uses_global_degree_not_subgraph_degree(self):
        interaction, protein = self._stats(
            source_ids=[self.narrow.pk], min_degree_global=5
        )
        # HUB has global degree 6, so it passes the gate even though only one of
        # its edges carries this source. Its partner on that edge has global
        # degree 1, so the edge is dropped — both endpoints must pass.
        self.assertEqual(interaction["n_interactions"], 0)
        self.assertEqual(protein["n_proteins"], 0)

    def test_reported_median_reflects_the_filtered_subgraph(self):
        # Without the degree gate, the single I2D edge survives and the reported
        # degree is 1 — far below any global-degree threshold a user might set.
        interaction, protein = self._stats(source_ids=[self.narrow.pk])
        self.assertEqual(interaction["n_interactions"], 1)
        self.assertEqual(protein["median_degree"], 1)
        # The same protein's global degree, which the gate reads, is 6.
        self.assertEqual(Protein.objects.get(pk=self.hub.pk).degree, 6)

    def test_gate_still_bites_on_the_unfiltered_graph(self):
        """Sanity check that min_degree_global is applied at all."""
        _, kept = self._stats(min_degree_global=5)
        self.assertEqual(kept["n_proteins"], 0)  # only HUB passes; partners do not
        _, all_proteins = self._stats()
        self.assertEqual(all_proteins["n_proteins"], 7)


class SplitParamsLegacyKeyTest(SimpleTestCase):
    """``min_degree`` was renamed to ``min_degree_global``.

    Browse hand-off links, bookmarked URLs and SplitJob rows written before the
    rename still carry the old key, so both spellings must land on the same
    field rather than raising.
    """

    def test_validate_accepts_both_spellings(self):
        from ..views import _validate_split_params

        params, invalid = _validate_split_params({"min_degree": 7})
        self.assertIsNone(invalid)
        self.assertEqual(params["min_degree_global"], 7)

        params, invalid = _validate_split_params({"min_degree_global": 7})
        self.assertIsNone(invalid)
        self.assertEqual(params["min_degree_global"], 7)

        # New key wins when both are present.
        params, invalid = _validate_split_params(
            {"min_degree": 1, "min_degree_global": 9}
        )
        self.assertIsNone(invalid)
        self.assertEqual(params["min_degree_global"], 9)

    def test_validate_rejects_a_negative_threshold(self):
        from ..views import _validate_split_params

        params, invalid = _validate_split_params({"min_degree": -1})
        self.assertIsNone(params)
        # The message has to name the constraint, not just signal failure — it is
        # what the caller sees in the 400 body.
        self.assertIn("min_degree_global", invalid)

    def test_from_payload_maps_a_stored_legacy_job(self):
        from ..services.generate_splits import SplitParams

        params = SplitParams.from_payload({"min_degree": 4, "min_score": 0.5})
        self.assertEqual(params.min_degree_global, 4)
        self.assertEqual(params.min_score, 0.5)


# ---------------------------------------------------------------------------
# Nextflow runner — the pure half, testable without a JVM
# ---------------------------------------------------------------------------


class NextflowRunnerUnitTest(SimpleTestCase):
    """Command construction, trace reading, and output collection.

    None of this needs Nextflow, which is exactly why it lives in
    services/nextflow_runner.py rather than inside the Celery task.
    """

    def test_command_pins_the_config_the_work_dir_and_the_run_name(self):
        from ..services import nextflow_runner as nf

        with tempfile.TemporaryDirectory() as td:
            layout = nf.job_layout("job-1", root=Path(td))
            cmd = nf.build_command(layout, "hippie_abc", pipeline=Path("/opt/pipeline"))

        self.assertEqual(cmd[0], "nextflow")
        self.assertIn("/opt/pipeline/main.nf", cmd)
        # split_only is passed as an explicit "true": Nextflow's strict parser
        # hands `--split_only` a String, and a bare flag with no value would
        # swallow the next argument.
        self.assertEqual(cmd[cmd.index("--split_only") + 1], "true")
        self.assertEqual(cmd[cmd.index("-name") + 1], "hippie_abc")
        self.assertEqual(cmd[cmd.index("-work-dir") + 1], str(layout.work))
        self.assertEqual(cmd[cmd.index("--outdir") + 1], str(layout.outdir))
        self.assertEqual(cmd[cmd.index("--samplesheet") + 1], str(layout.samplesheet))
        # -resume would only ever pick up this job's own failed attempt, and
        # keeping it meaningful would mean keeping every work dir forever.
        self.assertNotIn("-resume", cmd)
        # Escape codes instead of lines would make the log tail in job.error
        # unreadable.
        self.assertEqual(cmd[cmd.index("-ansi-log") + 1], "false")

    def test_step_label_tracks_the_latest_submitted_process(self):
        """Read from .nextflow.log, not the trace: a trace row appears only when
        a task finishes, so a run spending 30 minutes inside SOLVE_ILP would
        report "starting" for all of it."""
        from ..services import nextflow_runner as nf

        with tempfile.TemporaryDirectory() as td:
            layout = nf.job_layout("job-2", root=Path(td))
            layout.root.mkdir(parents=True)
            # No log yet — a job that has not reached its first task.
            self.assertEqual(nf.current_step(layout), "starting")

            # Real log shape: the process name is qualified by every enclosing
            # workflow, and PPI_SPLITTING itself is "submitted" before any task.
            layout.log.write_text(
                "Sep-02 22:57:20.1 [main] INFO  nextflow.Session - "
                "[a1/b2c3d4] Submitted process > PPI_SPLITTING\n"
                "Sep-02 22:57:21.8 [main] INFO  nextflow.Session - "
                "[bf/ede9a7] Submitted process > "
                "PPI_SPLITTING:SPLIT_POSITIVES:SORT_PPIS (hippie)\n"
                "Sep-02 23:29:02.4 [main] INFO  nextflow.Session - "
                "[c7/119aa2] Submitted process > "
                "PPI_SPLITTING:SAMPLE_NEGATIVES:SAMPLE_NEGATIVES_ILP (hippie_train)\n"
            )
            self.assertEqual(nf.current_step(layout), "sampling_negatives")

    def test_step_label_ignores_the_enclosing_workflow_name(self):
        """PPI_SPLITTING is submitted before any real task and is not a step."""
        from ..services import nextflow_runner as nf

        with tempfile.TemporaryDirectory() as td:
            layout = nf.job_layout("job-2b", root=Path(td))
            layout.root.mkdir(parents=True)
            layout.log.write_text("Submitted process > PPI_SPLITTING\n")
            self.assertEqual(nf.current_step(layout), "starting")

    def test_summary_counts_labels_and_distinct_proteins(self):
        from ..services import nextflow_runner as nf
        from ..services.export_ppis import ExportResult

        with tempfile.TemporaryDirectory() as td:
            layout = nf.job_layout("job-3", root=Path(td))
            layout.results.mkdir(parents=True)
            for name in nf.OUTPUT_FILES:
                (layout.results / name).write_text(
                    "protein1,protein2,label\nA,B,1\nB,C,1\nA,D,0\n"
                )
            export = ExportResult(
                path=layout.ppis,
                n_written=10,
                n_dropped_unknown=2,
                n_dropped_blank=0,
                n_proteins=4,
            )
            summary = nf.collect_summary(layout, export, "deadbeef")

        self.assertEqual(summary.n_positive_total, 8)  # 2 per file x 4 files
        self.assertEqual(summary.n_negative_total, 4)  # 1 per file x 4 files
        self.assertEqual(summary.n_proteins, 4)  # A, B, C, D
        self.assertEqual(summary.interactions_exported, 10)
        self.assertEqual(summary.interactions_dropped, 2)
        self.assertEqual(summary.pipeline_commit, "deadbeef")
        self.assertEqual(
            [s["name"] for s in summary.splits],
            ["train", "val", "test_balanced", "test_realistic"],
        )

    def test_summary_refuses_a_partial_publish(self):
        """Exit 0 without all four files is a pipeline contract break. Reporting
        DONE on three of them would hand out a silently incomplete dataset."""
        from ..services import nextflow_runner as nf
        from ..services.export_ppis import ExportResult

        with tempfile.TemporaryDirectory() as td:
            layout = nf.job_layout("job-4", root=Path(td))
            layout.results.mkdir(parents=True)
            for name in nf.OUTPUT_FILES[:-1]:
                (layout.results / name).write_text("protein1,protein2,label\nA,B,1\n")
            export = ExportResult(
                path=layout.ppis,
                n_written=1,
                n_dropped_unknown=0,
                n_dropped_blank=0,
                n_proteins=2,
            )
            with self.assertRaises(FileNotFoundError) as ctx:
                nf.collect_summary(layout, export, "x")
        self.assertIn(nf.OUTPUT_FILES[-1], str(ctx.exception))

    def test_error_report_includes_the_failing_task_stderr(self):
        """'terminated with an error exit status (1)' says nothing about whether
        the cause was a licence check-out, an OOM kill or an infeasibility."""
        from ..services import nextflow_runner as nf

        with tempfile.TemporaryDirectory() as td:
            layout = nf.job_layout("job-5", root=Path(td))
            layout.root.mkdir(parents=True)
            layout.log.write_text("ERROR ~ Process SAMPLE_NEGATIVES_ILP terminated\n")
            task_dir = layout.work / "ab" / "cdef01"
            task_dir.mkdir(parents=True)
            (task_dir / ".command.err").write_text("gurobipy: license expired\n")

            report = nf.error_report(layout, returncode=1)

        self.assertIn("status 1", report)
        self.assertIn("SAMPLE_NEGATIVES_ILP terminated", report)
        self.assertIn("license expired", report)


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class MLSplitCancelTest(TestCase):
    """A run holds the only execution slot for up to two hours, so a mis-clicked
    one has to be killable. PENDING and RUNNING cancel differently on purpose —
    see browse_splits_cancel."""

    def _post(self, job):
        return self.client.post(
            reverse("hippie_website:browse_splits_cancel", args=[job.pk])
        )

    def test_pending_job_is_cancelled_immediately_and_revoked(self):
        from unittest.mock import patch

        from ..models import SplitJob

        job = SplitJob.objects.create(params={}, status="PENDING")
        with patch("hippie_website.views.run_split_job.AsyncResult") as mock_result:
            resp = self._post(job)

        self.assertEqual(resp.status_code, 200)
        job.refresh_from_db()
        self.assertEqual(job.status, "CANCELLED")
        self.assertTrue(job.cancel_requested)
        self.assertIsNotNone(job.finished_at)
        # Revoke by job id — the task is enqueued with task_id == job id.
        mock_result.assert_called_once_with(str(job.id))
        mock_result.return_value.revoke.assert_called_once_with()

    def test_running_job_is_only_flagged_so_the_task_does_the_killing(self):
        from ..models import SplitJob

        job = SplitJob.objects.create(params={}, status="RUNNING")
        resp = self._post(job)

        self.assertEqual(resp.status_code, 202)
        job.refresh_from_db()
        self.assertTrue(job.cancel_requested)
        # Status is deliberately NOT written here: the task owns it, and a write
        # from the view would race the task's own and could be overwritten by a
        # DONE landing microseconds later.
        self.assertEqual(job.status, "RUNNING")

    def test_terminal_states_return_409(self):
        from ..models import SplitJob

        for status in ("DONE", "FAILED", "CANCELLED"):
            with self.subTest(status=status):
                job = SplitJob.objects.create(params={}, status=status)
                resp = self._post(job)
                self.assertEqual(resp.status_code, 409)
                self.assertIn(status, resp.json()["error"])
                job.refresh_from_db()
                self.assertFalse(job.cancel_requested)

    def test_cancel_404_for_unknown_job_id(self):
        import uuid

        resp = self.client.post(
            reverse("hippie_website:browse_splits_cancel", args=[uuid.uuid4()])
        )
        self.assertEqual(resp.status_code, 404)

    def test_status_reports_started_at_and_cancel_flag(self):
        from django.utils import timezone

        from ..models import SplitJob

        job = SplitJob.objects.create(
            params={},
            status="RUNNING",
            started_at=timezone.now(),
            cancel_requested=True,
        )
        resp = self.client.get(
            reverse("hippie_website:browse_splits_status", args=[job.pk])
        )
        payload = resp.json()
        self.assertIsNotNone(payload["started_at"])
        self.assertTrue(payload["cancel_requested"])


# ---------------------------------------------------------------------------
# The Celery task, with the subprocess mocked out
# ---------------------------------------------------------------------------


class RunSplitJobTaskTest(TestCase):
    """The task's own logic: the pre-start cancel check, the failure path's
    work-dir retention, and the success path's cleanup."""

    def test_a_job_cancelled_while_queued_never_launches_nextflow(self):
        """A worker that already prefetched the message ignores the revoke, so
        the flag has to be re-read here rather than trusted to Celery."""
        from unittest.mock import patch

        from ..models import SplitJob
        from ..tasks import run_split_job

        job = SplitJob.objects.create(
            params={}, status="CANCELLED", cancel_requested=True
        )
        with patch("subprocess.Popen") as popen:
            run_split_job(str(job.id))

        popen.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, "CANCELLED")
        self.assertIsNotNone(job.finished_at)

    def test_nonzero_exit_fails_the_job_and_keeps_the_work_dir(self):
        """.command.err and .nextflow.log are the only record of why a run died,
        and they are gone the moment the work dir is removed."""
        from unittest.mock import patch

        from ..models import SplitJob
        from ..services import nextflow_runner as nf
        from ..services.export_ppis import ExportResult
        from ..tasks import run_split_job

        job = SplitJob.objects.create(params={}, status="PENDING")

        with tempfile.TemporaryDirectory() as td:
            layout = nf.job_layout(str(job.id), root=Path(td))
            layout.work.mkdir(parents=True)
            layout.log.write_text("ERROR ~ boom\n")

            export = ExportResult(
                path=layout.ppis,
                n_written=5,
                n_dropped_unknown=0,
                n_dropped_blank=0,
                n_proteins=4,
            )
            proc = _FakeProc(returncode=2)

            with (
                patch("hippie_website.tasks.nf.preflight"),
                patch("hippie_website.tasks.nf.job_layout", return_value=layout),
                patch(
                    "hippie_website.tasks.nf.prepare_job_dir",
                    return_value=(export, "fp0123456789abcd"),
                ),
                patch("subprocess.Popen", return_value=proc),
                patch("hippie_website.tasks.POLL_SECONDS", 0),
            ):
                run_split_job(str(job.id))

            self.assertTrue(layout.work.is_dir(), "work dir must survive a failure")

        job.refresh_from_db()
        self.assertEqual(job.status, "FAILED")
        self.assertIn("status 2", job.error)
        self.assertIsNotNone(job.started_at)

    def test_an_empty_export_fails_before_launching_nextflow(self):
        """An empty ppis.csv would make the pipeline fail deep inside SOLVE_ILP
        with a message about the ILP, not about the filters that caused it."""
        from unittest.mock import patch

        from ..models import SplitJob
        from ..services import nextflow_runner as nf
        from ..services.export_ppis import ExportResult
        from ..tasks import run_split_job

        job = SplitJob.objects.create(params={}, status="PENDING")

        with tempfile.TemporaryDirectory() as td:
            layout = nf.job_layout(str(job.id), root=Path(td))
            export = ExportResult(
                path=layout.ppis,
                n_written=0,
                n_dropped_unknown=3,
                n_dropped_blank=0,
                n_proteins=0,
            )
            with (
                patch("hippie_website.tasks.nf.preflight"),
                patch("hippie_website.tasks.nf.job_layout", return_value=layout),
                patch(
                    "hippie_website.tasks.nf.prepare_job_dir",
                    return_value=(export, "fp0123456789abcd"),
                ),
                patch("subprocess.Popen") as popen,
                self.assertRaises(ValueError),
            ):
                run_split_job(str(job.id))
            popen.assert_not_called()

        job.refresh_from_db()
        self.assertEqual(job.status, "FAILED")
        self.assertIn("no interactions", job.error)

    def test_cancel_mid_run_kills_the_group_and_removes_the_job_dir(self):
        from unittest.mock import patch

        from ..models import SplitJob
        from ..services import nextflow_runner as nf
        from ..services.export_ppis import ExportResult
        from ..tasks import run_split_job

        job = SplitJob.objects.create(params={}, status="PENDING")

        with tempfile.TemporaryDirectory() as td:
            layout = nf.job_layout(str(job.id), root=Path(td))
            layout.work.mkdir(parents=True)
            export = ExportResult(
                path=layout.ppis,
                n_written=5,
                n_dropped_unknown=0,
                n_dropped_blank=0,
                n_proteins=4,
            )
            # Never exits on its own: only the cancel path can end this run.
            proc = _FakeProc(returncode=None, exits_after=None)

            def _flag_cancel(*_a, **_kw):
                SplitJob.objects.filter(pk=job.pk).update(cancel_requested=True)
                return proc

            with (
                patch("hippie_website.tasks.nf.preflight"),
                patch("hippie_website.tasks.nf.job_layout", return_value=layout),
                patch(
                    "hippie_website.tasks.nf.prepare_job_dir",
                    return_value=(export, "fp0123456789abcd"),
                ),
                patch("subprocess.Popen", side_effect=_flag_cancel),
                patch("hippie_website.tasks.POLL_SECONDS", 0),
                patch("hippie_website.tasks.os.killpg") as killpg,
            ):
                run_split_job(str(job.id))

            killpg.assert_called_once()
            self.assertFalse(layout.root.exists(), "job dir must be removed on cancel")

        job.refresh_from_db()
        self.assertEqual(job.status, "CANCELLED")
        self.assertIsNotNone(job.finished_at)


class _FakeProc:
    """Minimal subprocess.Popen stand-in for the task's poll loop."""

    def __init__(self, returncode=0, exits_after=1):
        self.pid = 4242
        self.returncode = returncode
        self._exits_after = exits_after
        self._polls = 0

    def poll(self):
        self._polls += 1
        if self._exits_after is None:
            return None
        return self.returncode if self._polls > self._exits_after else None

    def wait(self, timeout=None):
        self.returncode = self.returncode if self.returncode is not None else -15
        return self.returncode


class PipelineCommitTest(SimpleTestCase):
    """The commit is the actual reproducibility guarantee, so it must survive
    the one thing that reliably breaks it in this deployment."""

    def test_reads_head_from_a_bind_mounted_checkout(self):
        """The checkout is owned by the host user while the container runs as
        root, which trips git's dubious-ownership check. Without the scoped
        safe.directory every run would silently stamp "unknown"."""
        import subprocess

        from ..services import nextflow_runner as nf

        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "pipeline"
            repo.mkdir()
            env = {
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@e",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@e",
                "PATH": os.environ.get("PATH", ""),
                "HOME": td,
            }
            for cmd in (
                ["git", "init", "-q"],
                ["git", "commit", "-q", "--allow-empty", "-m", "x"],
            ):
                subprocess.run(cmd, cwd=repo, env=env, check=True)

            commit = nf.pipeline_commit(repo)

        self.assertRegex(commit, r"^[0-9a-f]{40}$")

    def test_a_checkout_without_git_metadata_degrades_instead_of_failing(self):
        """An archive export still produces a usable split; losing the stamp
        must not lose the run."""
        from ..services import nextflow_runner as nf

        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(nf.pipeline_commit(Path(td)), "unknown")


class ArtifactFingerprintTest(SimpleTestCase):
    """The proteome a split was cut from has to be identifiable from the split
    itself — an operator-maintained version string would not survive the manual
    out-of-band refresh these artifacts get."""

    def _artifacts(self, base: Path, sequences: bytes = b">A\nMK\n"):
        from ..services.export_ppis import ARTIFACT_FILES

        paths = {}
        for key, name in ARTIFACT_FILES.items():
            path = base / name
            path.write_bytes(sequences if key == "sequences" else b"x")
            paths[key] = path
        return paths

    def test_same_content_fingerprints_identically_across_directories(self):
        """Content, not mtime: the same proteome copied to another machine must
        produce the same value, or the fingerprint answers nothing."""
        from ..services.export_ppis import artifact_fingerprint

        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            fp_a = artifact_fingerprint(self._artifacts(Path(a)))
            fp_b = artifact_fingerprint(self._artifacts(Path(b)))
        self.assertEqual(fp_a, fp_b)
        self.assertRegex(fp_a, r"^[0-9a-f]{16}$")

    def test_changed_content_changes_the_fingerprint(self):
        from ..services.export_ppis import artifact_fingerprint

        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            fp_a = artifact_fingerprint(self._artifacts(Path(a)))
            fp_b = artifact_fingerprint(
                self._artifacts(Path(b), sequences=b">A\nMKV\n")
            )
        self.assertNotEqual(fp_a, fp_b)

    def test_summary_carries_the_fingerprint_into_the_zip(self):
        from ..services import nextflow_runner as nf
        from ..services.export_ppis import ExportResult

        with tempfile.TemporaryDirectory() as td:
            layout = nf.job_layout("job-fp", root=Path(td))
            layout.results.mkdir(parents=True)
            for name in nf.OUTPUT_FILES:
                (layout.results / name).write_text("protein1,protein2,label\nA,B,1\n")
            export = ExportResult(
                path=layout.ppis,
                n_written=1,
                n_dropped_unknown=0,
                n_dropped_blank=0,
                n_proteins=2,
            )
            summary = nf.collect_summary(layout, export, "abc", "fp0123456789abcd")
        self.assertEqual(summary.artifacts_fingerprint, "fp0123456789abcd")


# ---------------------------------------------------------------------------
# Queue transparency: position and the wait that position implies
# ---------------------------------------------------------------------------


class SplitQueueEstimateTest(TestCase):
    """The queue is uncapped and undeduplicated by choice, so the quoted wait is
    the only back-pressure a user gets. It has to be right."""

    def _job(self, status, created_offset=0, started=None, finished=None):
        from datetime import timedelta

        from django.utils import timezone

        from ..models import SplitJob

        job = SplitJob.objects.create(params={}, status=status)
        SplitJob.objects.filter(pk=job.pk).update(
            created_at=timezone.now() + timedelta(seconds=created_offset),
            started_at=started,
            finished_at=finished,
        )
        job.refresh_from_db()
        return job

    def test_typical_duration_falls_back_before_any_run_has_finished(self):
        from django.conf import settings

        from ..services.split_queue import typical_run_seconds

        self.assertEqual(typical_run_seconds(), settings.SPLIT_JOB_DEFAULT_RUN_SECONDS)

    def test_typical_duration_is_the_median_of_finished_runs(self):
        """Median, not mean: one four-hour outlier must not move every quote."""
        from datetime import timedelta

        from django.utils import timezone

        from ..services.split_queue import typical_run_seconds

        base = timezone.now() - timedelta(days=1)
        for minutes in (10, 20, 30, 240):
            self._job("DONE", started=base, finished=base + timedelta(minutes=minutes))
        # median of [600, 1200, 1800, 14400] = 1500
        self.assertEqual(typical_run_seconds(), 1500)

    def test_wait_counts_the_remainder_of_the_running_job_not_a_whole_one(self):
        """A queued user should watch the estimate fall as the job ahead
        finishes, not see it drop in one step when the slot frees."""
        from datetime import timedelta

        from django.utils import timezone

        from ..services.split_queue import estimated_wait_seconds, typical_run_seconds

        base = timezone.now() - timedelta(days=1)
        for _ in range(3):
            self._job("DONE", started=base, finished=base + timedelta(minutes=60))
        typical = typical_run_seconds()
        self.assertEqual(typical, 3600)

        # One run 50 minutes in, one job queued ahead of ours.
        self._job("RUNNING", started=timezone.now() - timedelta(minutes=50))
        self._job("PENDING", created_offset=-10)
        mine = self._job("PENDING", created_offset=0)

        wait = estimated_wait_seconds(mine)
        # 1 job ahead x 3600 + ~600 s left of the running one.
        self.assertAlmostEqual(wait, 3600 + 600, delta=30)

    def test_a_run_that_has_outlasted_the_estimate_contributes_zero(self):
        """Under-quoting beats an estimate that marches backwards past the run
        it is waiting on."""
        from datetime import timedelta

        from django.utils import timezone

        from ..services.split_queue import estimated_wait_seconds

        base = timezone.now() - timedelta(days=1)
        self._job("DONE", started=base, finished=base + timedelta(minutes=30))
        self._job("RUNNING", started=timezone.now() - timedelta(hours=5))
        mine = self._job("PENDING")

        self.assertEqual(estimated_wait_seconds(mine), 0)

    def test_status_endpoint_exposes_the_wait_and_nulls_it_once_started(self):
        from ..models import SplitJob

        pending = self._job("PENDING")
        running = SplitJob.objects.create(params={}, status="RUNNING")

        body = self.client.get(
            reverse("hippie_website:browse_splits_status", args=[pending.pk])
        ).json()
        self.assertIsNotNone(body["estimated_wait_seconds"])
        self.assertEqual(body["queue_position"], 0)

        body = self.client.get(
            reverse("hippie_website:browse_splits_status", args=[running.pk])
        ).json()
        self.assertIsNone(body["estimated_wait_seconds"])


# ---------------------------------------------------------------------------
# Housekeeping: reaping dead runs, pruning old ones, sweeping orphans
# ---------------------------------------------------------------------------


class SplitMaintenanceTest(TestCase):
    """Nothing else bounds the nf_work volume or the splits directory, and
    nothing else notices a run whose worker was killed."""

    def _run_maintenance(self, nf_root, media_root, **kwargs):
        from unittest.mock import patch

        from django.test import override_settings

        from ..services.split_maintenance import run_maintenance

        with (
            patch.dict(os.environ, {"NF_RUN_ROOT": str(nf_root)}),
            override_settings(MEDIA_ROOT=str(media_root)),
        ):
            return run_maintenance(**kwargs)

    def test_a_run_with_a_stale_heartbeat_is_failed_not_left_running(self):
        """A worker killed with its container leaves a row that polls forever
        and is counted by nothing."""
        from datetime import timedelta

        from django.utils import timezone

        from ..models import SplitJob
        from ..services.split_maintenance import MaintenanceReport, reap_stale_jobs

        dead = SplitJob.objects.create(params={}, status="RUNNING")
        SplitJob.objects.filter(pk=dead.pk).update(
            started_at=timezone.now() - timedelta(hours=3),
            heartbeat_at=timezone.now() - timedelta(hours=2),
        )
        alive = SplitJob.objects.create(params={}, status="RUNNING")
        SplitJob.objects.filter(pk=alive.pk).update(
            started_at=timezone.now() - timedelta(hours=3),
            heartbeat_at=timezone.now(),
        )

        reap_stale_jobs(MaintenanceReport(), timeout_seconds=900)

        dead.refresh_from_db()
        alive.refresh_from_db()
        self.assertEqual(dead.status, "FAILED")
        self.assertIn("no heartbeat", dead.error)
        self.assertIsNotNone(dead.finished_at)
        self.assertEqual(
            alive.status, "RUNNING", "a long run is not the same as a dead one"
        )

    def test_a_running_job_with_no_timestamps_at_all_is_left_alone(self):
        """Nothing to be stale against; guessing would risk failing a live run."""
        from ..models import SplitJob
        from ..services.split_maintenance import MaintenanceReport, reap_stale_jobs

        job = SplitJob.objects.create(params={}, status="RUNNING")
        reap_stale_jobs(MaintenanceReport(), timeout_seconds=1)
        job.refresh_from_db()
        self.assertEqual(job.status, "RUNNING")

    def test_prune_removes_old_terminal_jobs_with_their_zip_and_work_dir(self):
        from datetime import timedelta

        from django.utils import timezone

        from ..models import SplitJob

        with (
            tempfile.TemporaryDirectory() as nf_root,
            tempfile.TemporaryDirectory() as media,
        ):
            nf_root, media = Path(nf_root), Path(media)
            job = SplitJob.objects.create(params={}, status="DONE")
            root = nf_root / str(job.id)
            (root / "work").mkdir(parents=True)
            zip_path = media / "splits" / f"{job.id}.zip"
            zip_path.parent.mkdir(parents=True)
            zip_path.write_bytes(b"PK\x03\x04")
            SplitJob.objects.filter(pk=job.pk).update(
                finished_at=timezone.now() - timedelta(days=30),
                work_dir=str(root / "work"),
                zip_path=str(zip_path),
            )

            report = self._run_maintenance(nf_root, media, older_than_days=7)

            self.assertEqual(report.pruned, [str(job.id)])
            self.assertFalse(root.exists())
            self.assertFalse(zip_path.exists())
            self.assertFalse(SplitJob.objects.filter(pk=job.pk).exists())

    def test_prune_spares_queued_running_and_recent_jobs(self):
        """The queue is uncapped, so a PENDING job can legitimately be days old
        and still be waiting its turn — age alone must not collect it."""
        from datetime import timedelta

        from django.utils import timezone

        from ..models import SplitJob

        old = timezone.now() - timedelta(days=30)
        pending = SplitJob.objects.create(params={}, status="PENDING")
        running = SplitJob.objects.create(params={}, status="RUNNING")
        recent = SplitJob.objects.create(params={}, status="DONE")
        SplitJob.objects.filter(pk=pending.pk).update(created_at=old)
        SplitJob.objects.filter(pk=running.pk).update(
            created_at=old, started_at=timezone.now(), heartbeat_at=timezone.now()
        )
        SplitJob.objects.filter(pk=recent.pk).update(finished_at=timezone.now())

        with (
            tempfile.TemporaryDirectory() as nf_root,
            tempfile.TemporaryDirectory() as media,
        ):
            report = self._run_maintenance(
                Path(nf_root), Path(media), older_than_days=7
            )

        self.assertEqual(report.pruned, [])
        self.assertEqual(SplitJob.objects.count(), 3)

    def test_orphan_dirs_and_zips_with_no_job_row_are_swept(self):
        import uuid as _uuid

        from ..models import SplitJob

        with (
            tempfile.TemporaryDirectory() as nf_root,
            tempfile.TemporaryDirectory() as media,
        ):
            nf_root, media = Path(nf_root), Path(media)
            orphan_id = str(_uuid.uuid4())
            (nf_root / orphan_id).mkdir()
            (media / "splits").mkdir()
            (media / "splits" / f"{orphan_id}.zip").write_bytes(b"PK")

            # Neither of these is UUID-named, so neither is ours to remove.
            (nf_root / "not-a-job").mkdir()
            (media / "splits" / "README.txt").write_text("keep me")

            kept = SplitJob.objects.create(params={}, status="DONE")
            (nf_root / str(kept.id)).mkdir()

            report = self._run_maintenance(nf_root, media)

            self.assertEqual(report.orphan_dirs, [str(nf_root / orphan_id)])
            self.assertFalse((nf_root / orphan_id).exists())
            self.assertFalse((media / "splits" / f"{orphan_id}.zip").exists())
            self.assertTrue((nf_root / "not-a-job").is_dir())
            self.assertTrue((media / "splits" / "README.txt").is_file())
            self.assertTrue((nf_root / str(kept.id)).is_dir())

    def test_dry_run_reports_without_deleting_anything(self):
        from datetime import timedelta

        from django.utils import timezone

        from ..models import SplitJob

        with (
            tempfile.TemporaryDirectory() as nf_root,
            tempfile.TemporaryDirectory() as media,
        ):
            nf_root, media = Path(nf_root), Path(media)
            job = SplitJob.objects.create(params={}, status="FAILED")
            (nf_root / str(job.id) / "work").mkdir(parents=True)
            SplitJob.objects.filter(pk=job.pk).update(
                finished_at=timezone.now() - timedelta(days=30),
                work_dir=str(nf_root / str(job.id) / "work"),
            )

            report = self._run_maintenance(
                nf_root, media, older_than_days=7, dry_run=True
            )

            self.assertEqual(report.pruned, [str(job.id)])
            self.assertEqual(report.removed_dirs, [])
            self.assertTrue((nf_root / str(job.id)).is_dir())
            self.assertTrue(SplitJob.objects.filter(pk=job.pk).exists())

    def test_a_row_whose_work_dir_is_not_its_own_uuid_is_never_rmtreed(self):
        """_job_root hands a path straight to rmtree, so a malformed row must
        not be able to aim it somewhere else."""
        from datetime import timedelta

        from django.utils import timezone

        from ..models import SplitJob
        from ..services.split_maintenance import _job_root

        with tempfile.TemporaryDirectory() as nf_root:
            nf_root = Path(nf_root)
            victim = nf_root / "important"
            victim.mkdir()
            job = SplitJob.objects.create(params={}, status="DONE")
            SplitJob.objects.filter(pk=job.pk).update(
                finished_at=timezone.now() - timedelta(days=30),
                work_dir=str(victim / "work"),
            )
            job.refresh_from_db()

            self.assertIsNone(_job_root(job))

    def test_the_management_command_runs_and_reports(self):
        from io import StringIO

        from django.core.management import call_command

        with (
            tempfile.TemporaryDirectory() as nf_root,
            tempfile.TemporaryDirectory() as media,
        ):
            from unittest.mock import patch

            from django.test import override_settings

            out = StringIO()
            with (
                patch.dict(os.environ, {"NF_RUN_ROOT": nf_root}),
                override_settings(MEDIA_ROOT=media),
            ):
                call_command("prune_split_jobs", "--dry-run", stdout=out, stderr=out)
            self.assertIn("would prune", out.getvalue())


# ---------------------------------------------------------------------------
# acks_late: the same job can be delivered twice
# ---------------------------------------------------------------------------


class RunSplitJobRedeliveryTest(TestCase):
    """`acks_late` keeps the message on the broker for the whole run, and Redis
    re-queues anything unacked past visibility_timeout. Two hours of solver work
    must not be repeated, and a duplicate must not race the original."""

    def test_a_redelivery_on_top_of_a_live_run_is_a_no_op(self):
        from unittest.mock import patch

        from django.utils import timezone

        from ..models import SplitJob
        from ..tasks import run_split_job

        job = SplitJob.objects.create(params={}, status="RUNNING")
        SplitJob.objects.filter(pk=job.pk).update(
            started_at=timezone.now(), heartbeat_at=timezone.now()
        )

        with patch("subprocess.Popen") as popen:
            run_split_job(str(job.id))

        popen.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, "RUNNING")

    def test_a_redelivery_of_a_finished_job_is_a_no_op(self):
        from unittest.mock import patch

        from ..models import SplitJob
        from ..tasks import run_split_job

        for status in ("DONE", "FAILED"):
            job = SplitJob.objects.create(params={}, status=status)
            with patch("subprocess.Popen") as popen:
                run_split_job(str(job.id))
            popen.assert_not_called()
            job.refresh_from_db()
            self.assertEqual(job.status, status)

    def test_a_stale_running_job_is_retried_from_an_empty_directory(self):
        """The previous attempt died with its worker. Its half-written ppis.csv
        and work dir must not be inherited by a run that never produced them."""
        from datetime import timedelta
        from unittest.mock import patch

        from django.utils import timezone

        from ..models import SplitJob
        from ..services import nextflow_runner as nf
        from ..services.export_ppis import ExportResult
        from ..tasks import run_split_job

        job = SplitJob.objects.create(params={}, status="RUNNING")
        SplitJob.objects.filter(pk=job.pk).update(
            started_at=timezone.now() - timedelta(hours=3),
            heartbeat_at=timezone.now() - timedelta(hours=2),
        )

        with tempfile.TemporaryDirectory() as td:
            layout = nf.job_layout(str(job.id), root=Path(td))
            layout.work.mkdir(parents=True)
            stale_marker = layout.root / "ppis.csv"
            stale_marker.write_text("half,written\n")

            export = ExportResult(
                path=layout.ppis,
                n_written=5,
                n_dropped_unknown=0,
                n_dropped_blank=0,
                n_proteins=4,
            )

            def _prepare(_params, _layout, *a, **kw):
                self.assertFalse(
                    stale_marker.exists(),
                    "the previous attempt's directory must be gone before the retry",
                )
                _layout.root.mkdir(parents=True, exist_ok=True)
                _layout.work.mkdir(parents=True, exist_ok=True)
                _layout.log.write_text("ERROR ~ boom\n")
                return export, "fp0123456789abcd"

            with (
                patch("hippie_website.tasks.nf.preflight"),
                patch("hippie_website.tasks.nf.job_layout", return_value=layout),
                patch("hippie_website.tasks.nf.prepare_job_dir", side_effect=_prepare),
                patch("subprocess.Popen", return_value=_FakeProc(returncode=2)),
                patch("hippie_website.tasks.POLL_SECONDS", 0),
            ):
                run_split_job(str(job.id))

        job.refresh_from_db()
        self.assertEqual(job.status, "FAILED")

    def test_the_poll_loop_writes_a_heartbeat_on_every_pass(self):
        """The beat is the liveness signal, so it cannot ride on `step` changing
        — a run spends half an hour inside SOLVE_ILP without changing step."""
        from datetime import timedelta
        from unittest.mock import patch

        from django.utils import timezone

        from ..models import SplitJob
        from ..services import nextflow_runner as nf
        from ..services.export_ppis import ExportResult
        from ..tasks import run_split_job

        job = SplitJob.objects.create(params={}, status="PENDING")

        class _BackdatingProc(_FakeProc):
            """Ages the heartbeat before the loop body gets a chance to refresh
            it, so a loop that does not write one leaves it visibly stale."""

            def poll(self):
                result = super().poll()
                if result is None:
                    SplitJob.objects.filter(pk=job.pk).update(
                        heartbeat_at=timezone.now() - timedelta(hours=2)
                    )
                return result

        with tempfile.TemporaryDirectory() as td:
            layout = nf.job_layout(str(job.id), root=Path(td))
            layout.work.mkdir(parents=True)
            layout.log.write_text("ERROR ~ boom\n")
            export = ExportResult(
                path=layout.ppis,
                n_written=5,
                n_dropped_unknown=0,
                n_dropped_blank=0,
                n_proteins=4,
            )
            with (
                patch("hippie_website.tasks.nf.preflight"),
                patch("hippie_website.tasks.nf.job_layout", return_value=layout),
                patch(
                    "hippie_website.tasks.nf.prepare_job_dir",
                    return_value=(export, "fp0123456789abcd"),
                ),
                patch(
                    "subprocess.Popen",
                    return_value=_BackdatingProc(returncode=2, exits_after=2),
                ),
                patch("hippie_website.tasks.POLL_SECONDS", 0),
            ):
                run_split_job(str(job.id))

        job.refresh_from_db()
        self.assertIsNotNone(job.heartbeat_at)
        self.assertGreater(job.heartbeat_at, timezone.now() - timedelta(minutes=5))


# ---------------------------------------------------------------------------
# Preflight: refuse a run that cannot succeed, before the JVM starts
# ---------------------------------------------------------------------------


class PreflightTest(SimpleTestCase):
    """Each of these failures otherwise surfaces twenty minutes into SOLVE_ILP,
    times the retry ladder, as an error naming a path inside a work dir."""

    def _env(self, pipeline, config, licence):
        from unittest.mock import patch

        return (
            patch(
                "hippie_website.services.nextflow_runner.pipeline_dir",
                return_value=pipeline,
            ),
            patch(
                "hippie_website.services.nextflow_runner.hippie_config",
                return_value=config,
            ),
            patch(
                "hippie_website.services.nextflow_runner.gurobi_license",
                return_value=licence,
            ),
        )

    def _ok_tree(self, td):
        pipeline = Path(td) / "pipeline"
        pipeline.mkdir()
        (pipeline / "main.nf").write_text("workflow {}\n")
        config = Path(td) / "hippie.config"
        config.write_text("params {}\n")
        licence = Path(td) / "gurobi.lic"
        licence.write_text("WLSACCESSID=x\n")
        return pipeline, config, licence

    def _run(self, pipeline, config, licence):
        from ..services.nextflow_runner import preflight

        a, b, c = self._env(pipeline, config, licence)
        with a, b, c:
            preflight()

    def test_a_complete_deployment_passes(self):
        with tempfile.TemporaryDirectory() as td:
            self._run(*self._ok_tree(td))

    def test_an_unfetched_submodule_is_named_as_such(self):
        from ..services.nextflow_runner import PreflightError

        with tempfile.TemporaryDirectory() as td:
            pipeline, config, licence = self._ok_tree(td)
            (pipeline / "main.nf").unlink()
            with self.assertRaises(PreflightError) as ctx:
                self._run(pipeline, config, licence)
            self.assertIn("submodule", str(ctx.exception))

    def test_a_licence_that_docker_turned_into_a_directory_is_diagnosed(self):
        """The real failure mode: Docker creates a directory at both ends of a
        bind mount whose host path is missing, Nextflow's checkIfExists is happy
        with it, and Gurobi is the first thing in the chain to notice."""
        from ..services.nextflow_runner import PreflightError

        with tempfile.TemporaryDirectory() as td:
            pipeline, config, licence = self._ok_tree(td)
            licence.unlink()
            licence.mkdir()
            with self.assertRaises(PreflightError) as ctx:
                self._run(pipeline, config, licence)
            message = str(ctx.exception)
            self.assertIn("directory, not a file", message)
            self.assertIn("GUROBI_LICENSE_PATH", message)

    def test_a_missing_licence_is_diagnosed(self):
        from ..services.nextflow_runner import PreflightError

        with tempfile.TemporaryDirectory() as td:
            pipeline, config, licence = self._ok_tree(td)
            licence.unlink()
            with self.assertRaises(PreflightError) as ctx:
                self._run(pipeline, config, licence)
            self.assertIn("No Gurobi licence", str(ctx.exception))

    def test_an_empty_licence_is_diagnosed(self):
        from ..services.nextflow_runner import PreflightError

        with tempfile.TemporaryDirectory() as td:
            pipeline, config, licence = self._ok_tree(td)
            licence.write_text("")
            with self.assertRaises(PreflightError) as ctx:
                self._run(pipeline, config, licence)
            self.assertIn("empty", str(ctx.exception))

    def test_a_missing_config_is_diagnosed(self):
        from ..services.nextflow_runner import PreflightError

        with tempfile.TemporaryDirectory() as td:
            pipeline, config, licence = self._ok_tree(td)
            config.unlink()
            with self.assertRaises(PreflightError) as ctx:
                self._run(pipeline, config, licence)
            self.assertIn("parameter file", str(ctx.exception))


class PreflightFailsTheJobCleanlyTest(TestCase):
    def test_the_run_card_shows_the_diagnosis_not_a_traceback(self):
        from unittest.mock import patch

        from ..models import SplitJob
        from ..services.nextflow_runner import PreflightError
        from ..tasks import run_split_job

        job = SplitJob.objects.create(params={}, status="PENDING")
        with (
            patch(
                "hippie_website.tasks.nf.preflight",
                side_effect=PreflightError("no licence, and here is why"),
            ),
            patch("subprocess.Popen") as popen,
            self.assertRaises(PreflightError),
        ):
            run_split_job(str(job.id))

        popen.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, "FAILED")
        self.assertEqual(job.error, "no licence, and here is why")
        self.assertNotIn("Traceback", job.error)
