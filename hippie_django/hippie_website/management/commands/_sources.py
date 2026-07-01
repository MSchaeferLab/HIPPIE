"""
Shared accessor for ``data/sources.json`` — the single source of truth for every
external dataset the HIPPIE update routine uses (download addresses + the version
currently sitting in ``data/``).

Underscore prefix → Django's management-command discovery ignores this module, but it
is importable by sibling commands the same way ``update_homology_data`` imports from
``hippie_update``.

The companion ``download_update_data.sh`` reads the same JSON (via an inline ``python3``
helper) and writes the ``fetched`` / ``last_modified`` / ``etag`` fields back after each
download. Python live-fetch paths can stamp those fields too via :func:`record_fetch`.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

def _resolve_config() -> Path:
    """
    Locate ``sources.json``. Priority:
      1. ``$HIPPIE_SOURCES_CONFIG`` (explicit override),
      2. ``project_root/data/sources.json`` — the deployed layout, where this file is at
         ``project_root/<app>/management/commands/_sources.py`` (parents[3] = root),
      3. ``data/sources.json`` next to this module (flat working copy / tests).
    """
    override = os.environ.get("HIPPIE_SOURCES_CONFIG")
    if override:
        return Path(override).resolve()
    deployed = Path(__file__).resolve().parents[3] / "data" / "sources.json"
    if deployed.exists():
        return deployed
    sibling = Path(__file__).resolve().parent / "data" / "sources.json"
    if sibling.exists():
        return sibling
    return deployed  # canonical location; error surfaces on first read


CONFIG_PATH = _resolve_config()
DATA_DIR = CONFIG_PATH.parent

_cache: dict[str, Any] | None = None


def load_sources(*, refresh: bool = False) -> dict[str, dict[str, Any]]:
    """Return the ``sources`` mapping from ``data/sources.json`` (cached)."""
    global _cache
    if _cache is None or refresh:
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            _cache = json.load(f)
    return _cache["sources"]


def _entry(key: str) -> dict[str, Any]:
    try:
        return load_sources()[key]
    except KeyError:
        raise KeyError(
            f"Unknown data source {key!r}. Known keys: "
            f"{', '.join(sorted(load_sources()))}"
        ) from None


def data_path(key: str) -> Path:
    """Absolute path to the local file for ``key`` inside the ``data/`` directory."""
    entry = _entry(key)
    filename = entry.get("filename")
    if not filename:
        raise KeyError(f"Source {key!r} has no 'filename' (it is fetched at runtime).")
    return DATA_DIR / filename


def source_url(key: str) -> str:
    """Upstream download URL for ``key`` (the artifact the download script fetches)."""
    url = _entry(key).get("url")
    if not url:
        raise KeyError(f"Source {key!r} has no 'url' (local/generated file).")
    return url


def stream_url(key: str) -> str:
    """
    URL for streaming ``key`` live (uncompressed). Falls back to ``url`` when no separate
    ``stream_url`` is configured. Used where a command reads upstream without first
    downloading + decompressing (e.g. the live IntAct MITAB stream).
    """
    entry = _entry(key)
    url = entry.get("stream_url") or entry.get("url")
    if not url:
        raise KeyError(f"Source {key!r} has no 'stream_url'/'url'.")
    return url


def mod_sources() -> dict[str, dict[str, Any]]:
    """
    Reconstruct the taxon-keyed MOD/ortholog source map consumed by
    ``update_homology_data`` from the config.

    Returns ``{taxon: {"db": ..., "url"|"file": ..., "gz": 0|1}}``. WormBase
    (``manual``) resolves to a local ``file`` path; the rest carry their live ``url``.
    """
    out: dict[str, dict[str, Any]] = {}
    for entry in load_sources().values():
        taxon = entry.get("taxon")
        if not taxon:
            continue
        conf: dict[str, Any] = {"db": entry["db"], "gz": entry.get("gz", 0)}
        if entry.get("manual"):
            conf["file"] = str(DATA_DIR / entry["filename"])
        else:
            conf["url"] = entry["url"]
        out[taxon] = conf
    return out


def record_fetch(
    key: str,
    *,
    fetched: str | None = None,
    last_modified: str | None = None,
    etag: str | None = None,
) -> None:
    """
    Stamp the version metadata for ``key`` and rewrite ``data/sources.json`` atomically.

    ``fetched`` defaults to today's ISO date. Only non-None values overwrite existing
    fields, so a missing ``Last-Modified`` / ``ETag`` header leaves the prior value.
    """
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        doc = json.load(f)
    entry = doc["sources"][key]
    entry["fetched"] = fetched or date.today().isoformat()
    if last_modified is not None:
        entry["last_modified"] = last_modified
    if etag is not None:
        entry["etag"] = etag

    fd, tmp = tempfile.mkstemp(dir=str(CONFIG_PATH.parent), suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, CONFIG_PATH)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    global _cache
    _cache = None  # invalidate
