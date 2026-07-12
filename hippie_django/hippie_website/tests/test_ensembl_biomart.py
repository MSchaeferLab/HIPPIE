"""Tests for the Ensembl enrichment hardening + BioMart bulk tier.

Covers three things the DIGGER suite (which only exercises the local ``skip_api``
path) does not:

* the pure BioMart TSV parser (:func:`_biomart.parse_biomart_tsv`), no network;
* the ``_ensembl_json`` crash regression — a dropped connection must be retried
  and swallowed, never propagated (the original ``RemoteDisconnected`` bug);
* the BioMart Tier-2 fill inside ``_populate_ensembl_ids`` — gaps left by the
  local pass are filled from BioMart, and the per-item REST tier is not touched
  when nothing remains.
"""

import http.client
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from django.core.management.base import OutputWrapper
from django.test import SimpleTestCase

from ..management.commands import _biomart, hippie_update
from ..models import Isoform
from .factories import HippieTestCase


# ---------------------------------------------------------------------------
# BioMart TSV parser (pure, no network)
# ---------------------------------------------------------------------------

_BIOMART_TSV = "\n".join(
    [
        # acc            ENSG               ENST                ENSP                iso
        "P04637\tENSG00000141510.19\tENST00000269305.4\tENSP00000269305.4\tP04637-1",
        "P04637\tENSG00000141510\tENST00000420246\tENSP00000391127\tP04637-2",
        # empty uniprot_isoform -> ENSG still collected, nothing added to iso map
        "P00533\tENSG00000146648.1\tENST00000275493\tENSP00000275493\t",
        "",  # blank line ignored
    ]
)


class BiomartParseTest(SimpleTestCase):
    def test_parse_strips_versions_and_keys_by_isoform(self):
        iso_map, acc_ensg = _biomart.parse_biomart_tsv(_BIOMART_TSV)
        self.assertEqual(iso_map["P04637-1"]["enst"], ["ENST00000269305"])
        self.assertEqual(iso_map["P04637-1"]["ensp"], ["ENSP00000269305"])
        self.assertEqual(iso_map["P04637-2"]["enst"], ["ENST00000420246"])
        self.assertEqual(iso_map["P04637-2"]["ensp"], ["ENSP00000391127"])

    def test_parse_skips_empty_isoform_but_keeps_ensg(self):
        iso_map, acc_ensg = _biomart.parse_biomart_tsv(_BIOMART_TSV)
        self.assertNotIn("", iso_map)
        # The P00533 row has no uniprot_isoform, so it contributes only its ENSG.
        self.assertEqual(acc_ensg["P00533"], ["ENSG00000146648"])

    def test_parse_ensg_not_deduped(self):
        # Two P04637 rows -> two (version-stripped) ENSG entries; caller de-dupes.
        _, acc_ensg = _biomart.parse_biomart_tsv(_BIOMART_TSV)
        self.assertEqual(acc_ensg["P04637"], ["ENSG00000141510", "ENSG00000141510"])

    def test_build_query_xml_embeds_filter_and_values(self):
        xml = _biomart.build_query_xml("uniprotswissprot", ["P04637", "P38398"])
        self.assertIn('name="uniprotswissprot" value="P04637,P38398"', xml)
        self.assertIn('name="uniprot_isoform"', xml)


# ---------------------------------------------------------------------------
# _ensembl_json crash regression
# ---------------------------------------------------------------------------


class EnsemblJsonRetryTest(SimpleTestCase):
    def test_remote_disconnected_is_retried_then_returns_none(self):
        """A dropped connection must be caught + retried, never raised.

        ``http.client.RemoteDisconnected`` is an ``OSError`` (not a
        ``urllib.error.URLError``) and is raised inside ``getresponse()``, which
        urllib does not wrap — the exact escape that used to crash the command.
        """
        boom = http.client.RemoteDisconnected(
            "Remote end closed connection without response"
        )
        with (
            mock.patch("urllib.request.urlopen", side_effect=boom) as urlopen,
            mock.patch.object(hippie_update.time, "sleep"),  # no real backoff waits
        ):
            result = hippie_update._ensembl_json("/xrefs/x", throttle=[0.0])
        self.assertIsNone(result)
        self.assertEqual(urlopen.call_count, 5)  # all attempts exhausted, no raise


# ---------------------------------------------------------------------------
# BioMart Tier-2 fill inside _populate_ensembl_ids
# ---------------------------------------------------------------------------

# Local idmapping that fills all three canonical genes' ENSG, so the only gap the
# network tiers see is the isoform created in the test below.
_LOCAL_FIXTURE = "\n".join(
    [
        "P38398\tEnsembl\tENSG00000012048.1",
        "P04637\tEnsembl\tENSG00000141510.19",
        "P00533\tEnsembl\tENSG00000146648.1",
    ]
)


class BiomartFillTest(HippieTestCase):
    def test_gap_isoform_filled_from_biomart_without_rest(self):
        iso = Isoform.objects.create(
            uniprot_accession="P37163-2",
            general_protein=self.brca1,
            gene=self.brca1.gene,
            uniprot_name="P37163_2",
        )
        biomart_return = (
            {"P37163-2": {"enst": ["ENST00000376389"], "ensp": ["ENSP00000365569"]}},
            {},  # no gene ENSG gaps — the local pass already filled them
        )
        with TemporaryDirectory() as d:
            path = Path(d) / "idmapping.dat"
            path.write_text(_LOCAL_FIXTURE + "\n")
            with (
                mock.patch.object(hippie_update, "data_path", return_value=path),
                mock.patch.object(
                    hippie_update._biomart,
                    "fetch_uniprot_ensembl_map",
                    return_value=biomart_return,
                ) as fetch,
                mock.patch.object(hippie_update, "_ensembl_json") as ensembl_json,
            ):
                hippie_update._populate_ensembl_ids(
                    OutputWrapper(StringIO()), skip_api=False
                )

        iso.refresh_from_db()
        self.assertEqual(iso.enst, ["ENST00000376389"])
        self.assertEqual(iso.ensp, ["ENSP00000365569"])
        # BioMart was queried once, with the isoform's *base* accession.
        fetch.assert_called_once()
        queried = fetch.call_args.args[0]
        self.assertIn("P37163", queried)
        # Everything resolved locally + via BioMart, so the per-item REST tier
        # never fired.
        ensembl_json.assert_not_called()

    def test_biomart_failure_is_non_fatal_and_falls_through_to_rest(self):
        Isoform.objects.create(
            uniprot_accession="P37163-2",
            general_protein=self.brca1,
            gene=self.brca1.gene,
            uniprot_name="P37163_2",
        )
        with TemporaryDirectory() as d:
            path = Path(d) / "idmapping.dat"
            path.write_text(_LOCAL_FIXTURE + "\n")
            with (
                mock.patch.object(hippie_update, "data_path", return_value=path),
                mock.patch.object(
                    hippie_update._biomart,
                    "fetch_uniprot_ensembl_map",
                    side_effect=RuntimeError("BioMart down"),
                ),
                mock.patch.object(
                    hippie_update, "_ensembl_json", return_value=None
                ) as ensembl_json,
            ):
                # Must not raise; REST tier still runs over the remaining gap.
                hippie_update._populate_ensembl_ids(
                    OutputWrapper(StringIO()), skip_api=False
                )
        ensembl_json.assert_called()  # fell through to the REST fallback
