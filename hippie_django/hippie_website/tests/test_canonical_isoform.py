"""Tests for canonical-isoform detection in ``hippie_update``.

``_populate_canonical_isoforms`` flags an :class:`Isoform` as canonical when its
UniParc sequence hash matches its base (canonical) accession's UniParc. UniProt's
idmapping file has no explicit canonical marker, so UniParc identity is the
signal. These tests drive the real parser + populate through a temporary
idmapping fixture (same pattern as ``test_ensembl_biomart``), covering the
match / no-match cases, the >1-UniParc set-intersection, and the non-fatal
multi-canonical warning.
"""

from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from django.core.management.base import OutputWrapper

from ..management.commands import hippie_update
from ..models import Isoform
from .factories import HippieTestCase


class CanonicalIsoformTest(HippieTestCase):
    # ``self.brca1`` (accession P38398) is the canonical parent; isoforms are
    # created as P38398-N so their base accession resolves back to it.

    def _iso(self, acc: str) -> Isoform:
        return Isoform.objects.create(
            uniprot_accession=acc,
            general_protein=self.brca1,
            gene=self.brca1.gene,
            uniprot_name=acc.replace("-", "_"),
        )

    def _run(self, lines: list[str]) -> str:
        """Write an idmapping fixture, run the populate step, return stdout."""
        buf = StringIO()
        with TemporaryDirectory() as d:
            path = Path(d) / "idmapping.dat"
            path.write_text("\n".join(lines) + "\n")
            with mock.patch.object(hippie_update, "data_path", return_value=path):
                hippie_update._populate_canonical_isoforms(OutputWrapper(buf))
        return buf.getvalue()

    def test_uniparc_match_flags_only_the_canonical_isoform(self):
        iso1 = self._iso("P38398-1")
        iso2 = self._iso("P38398-2")
        self._run(
            [
                "P38398\tUniParc\tUPI_A",  # base canonical sequence
                "P38398-1\tUniParc\tUPI_A",  # same sequence -> canonical
                "P38398-2\tUniParc\tUPI_B",  # different sequence
            ]
        )
        iso1.refresh_from_db()
        iso2.refresh_from_db()
        self.assertIs(iso1.is_canonical, True)
        self.assertIs(iso2.is_canonical, False)

    def test_canonical_is_not_always_dash_one(self):
        """The UniParc rule must beat the naive ``-1`` guess: here ``-2`` is
        canonical because it, not ``-1``, shares the base sequence."""
        iso1 = self._iso("P38398-1")
        iso2 = self._iso("P38398-2")
        self._run(
            [
                "P38398\tUniParc\tUPI_A",
                "P38398-1\tUniParc\tUPI_B",
                "P38398-2\tUniParc\tUPI_A",  # matches base -> canonical
            ]
        )
        iso1.refresh_from_db()
        iso2.refresh_from_db()
        self.assertIs(iso1.is_canonical, False)
        self.assertIs(iso2.is_canonical, True)

    def test_multiple_uniparc_rows_use_set_intersection(self):
        """An accession may carry >1 UniParc row; an intersection with the base
        set still resolves canonical correctly."""
        iso = self._iso("P38398-1")
        self._run(
            [
                "P38398\tUniParc\tUPI_A",
                "P38398-1\tUniParc\tUPI_Z",
                "P38398-1\tUniParc\tUPI_A",  # one of two matches base
            ]
        )
        iso.refresh_from_db()
        self.assertIs(iso.is_canonical, True)

    def test_isoform_absent_from_idmapping_is_not_canonical(self):
        iso = self._iso("P38398-2")  # no UniParc rows for it in the fixture
        self._run(["P38398\tUniParc\tUPI_A"])
        iso.refresh_from_db()
        self.assertIs(iso.is_canonical, False)

    def test_multiple_canonical_matches_warn_without_failing(self):
        """Two isoforms sharing the base UniParc must both be flagged and a
        non-fatal WARNING emitted — the step must not raise."""
        iso1 = self._iso("P38398-1")
        iso2 = self._iso("P38398-2")
        out = self._run(
            [
                "P38398\tUniParc\tUPI_A",
                "P38398-1\tUniParc\tUPI_A",
                "P38398-2\tUniParc\tUPI_A",
            ]
        )
        iso1.refresh_from_db()
        iso2.refresh_from_db()
        self.assertIs(iso1.is_canonical, True)
        self.assertIs(iso2.is_canonical, True)
        self.assertIn("WARNING", out)
        self.assertIn("P38398-1", out)
        self.assertIn("P38398-2", out)
