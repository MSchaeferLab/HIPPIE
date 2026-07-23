"""Shared reader for PSI-MI TAB (MITAB) interaction files.

Both the HIPPIE update pipeline (IntAct / BioGRID, ``hippie_update``) and the
homology-data importer (IntAct ortholog stream, ``update_homology_data``) read
the same tab-delimited format. ``open_mitab`` centralises the
open / decompress / decode / comment-skip / tab-split plumbing; each caller
keeps its own per-column filtering.

Underscore-prefixed so Django's management-command discovery ignores it (same
convention as ``_sources.py``).
"""

import gzip
import io
import urllib.request
from collections.abc import Iterator


def _looks_like_url(path_or_url: str) -> bool:
    return path_or_url.startswith(("http://", "https://", "ftp://", "ftps://"))


def open_mitab(path_or_url: str) -> Iterator[list[str]]:
    """Yield the tab-split fields of each data line of a MITAB file.

    ``path_or_url`` may be a local path (plain or gzip — decompressed when the
    name ends in ``.gz``) or an http(s)/ftp URL (read as-is; the existing IntAct
    stream is served uncompressed). Blank lines and lines starting with ``#``
    are skipped; the caller handles any format-specific header row (e.g. the
    ``ID(s) interactor A`` line) and per-column filtering.
    """
    if _looks_like_url(path_or_url):
        raw = urllib.request.urlopen(
            urllib.request.Request(
                path_or_url, headers={"User-Agent": "protein-mapper/1.0"}
            ),
            timeout=120,
        )
    else:
        raw = open(path_or_url, "rb")
        if path_or_url.endswith(".gz"):
            raw = gzip.GzipFile(fileobj=raw)

    text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace")
    try:
        for line in text:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            yield line.split("\t")
    finally:
        text.close()
