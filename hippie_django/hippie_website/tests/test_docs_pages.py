"""Documentation pages: information, filter guide, REST API, ML guide, banner."""

import datetime

from django.test import TestCase
from django.urls import reverse

from ..models import ExperimentType, ReleaseMeta, Source


class DocsPagesTest(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        Source.objects.create(
            name="bhf-ucl",
            url="https://example.org/bhf",
            description="British Heart Foundation – University College London",
            n_connected_interactions=1720,
        )
        Source.objects.create(name="IntAct", n_connected_interactions=524211)
        # Unused sources stay out of the counts table.
        Source.objects.create(name="hprd", n_connected_interactions=0)
        ExperimentType.objects.create(
            name="two hybrid", psi_mi_code="MI:0018", n_connected_interactions=5
        )

    def _get(self, name: str) -> str:
        resp = self.client.get(reverse(f"hippie_website:{name}"))
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def test_information_has_no_placeholders(self) -> None:
        html = self._get("information")
        for marker in ("FORMULA", "COPY HIPPIE USE CASE", "REWORK NEEDED"):
            self.assertNotIn(marker, html)
        self.assertIn('id="scoring"', html)
        self.assertIn("?q=HTT", html)

    def test_information_sources_come_from_the_db(self) -> None:
        html = self._get("information")
        self.assertIn("2 data sources (1 curation team, 1 interaction database)", html)
        self.assertIn("1720", html)
        self.assertIn("524211", html)
        self.assertNotIn(">hprd<", html)

    def test_information_moved_api_and_filters_out(self) -> None:
        html = self._get("information")
        self.assertNotIn("/api/query/", html)
        self.assertNotIn('id="filters"', html)
        self.assertIn(reverse("hippie_website:filter_guide"), html)
        self.assertIn(reverse("hippie_website:api_docs"), html)

    def test_filter_guide_anchors_and_acronyms(self) -> None:
        html = self._get("filter_guide")
        for anchor in (
            "result-type",
            "confidence",
            "sources",
            "experiments",
            "interaction-types",
            "tissues",
            "protein-filters",
            "reviewed",
            "isoforms",
            "ml-splits",
        ):
            self.assertIn(f'id="{anchor}"', html)
        self.assertIn("British Heart Foundation", html)
        self.assertIn("obo_id=MI:0018", html)

    def test_api_page_lists_every_endpoint(self) -> None:
        html = self._get("api_docs")
        for path in (
            "/api/query/",
            "/api/interaction/",
            "/api/network/",
            "/api/browse/proteins/",
            "/api/browse/export/",
            "/api/browse/splits/raw/",
        ):
            self.assertIn(path, html)

    def test_ml_guide_has_no_placeholders(self) -> None:
        html = self._get("machine_learning")
        self.assertNotIn("LEFT OFF HERE", html)
        self.assertIn('id="key-reading"', html)
        self.assertIn("--gurobi_license", html)

    def test_information_dropdown_in_top_bar(self) -> None:
        html = self._get("download")
        self.assertIn('class="dropdown-menu', html)
        self.assertIn(reverse("hippie_website:filter_guide"), html)


class ReleaseBannerTest(TestCase):
    def test_banner_without_release(self) -> None:
        html = self.client.get(reverse("hippie_website:download")).content.decode()
        self.assertIn("hippie-release-missing", html)

    def test_no_banner_with_release(self) -> None:
        ReleaseMeta.objects.create(
            release_number=1,
            version_label="3.0",
            release_date=datetime.date(2026, 6, 17),
            int_median=0.63,
            int_q3=0.73,
        )
        html = self.client.get(reverse("hippie_website:download")).content.decode()
        self.assertNotIn("hippie-release-missing", html)
