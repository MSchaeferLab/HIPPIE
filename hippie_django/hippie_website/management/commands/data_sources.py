"""
Management command: data_sources

Report the data sources declared in ``data/sources.json`` — the upstream address and,
crucially, *which version is currently in use* (the declared upstream release tag plus
the fetch metadata stamped in by ``download_update_data.sh`` on the last download).

Usage:
    python manage.py data_sources           # human-readable table
    python manage.py data_sources --json     # machine-readable JSON
    python manage.py data_sources --missing   # only sources whose local file is absent
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from ._sources import CONFIG_PATH, DATA_DIR, load_sources


def _kind(entry: dict) -> str:
    if entry.get("manual"):
        return "manual"
    if entry.get("runtime"):
        return "runtime"
    if entry.get("local"):
        return "local"
    return "download"


class Command(BaseCommand):
    help = "Show the configured data sources and the version currently in use."

    def add_arguments(self, parser):
        parser.add_argument(
            "--json", action="store_true", help="Emit machine-readable JSON."
        )
        parser.add_argument(
            "--missing",
            action="store_true",
            help="Only list sources whose local file is missing from the data directory.",
        )

    def handle(self, *args, **options):
        sources = load_sources()

        rows = []
        for key, entry in sources.items():
            filename = entry.get("filename")
            local_present = bool(filename) and (DATA_DIR / filename).exists()
            if options["missing"] and (not filename or local_present):
                continue
            rows.append(
                {
                    "key": key,
                    "kind": _kind(entry),
                    "version": entry.get("version"),
                    "fetched": entry.get("fetched"),
                    "last_modified": entry.get("last_modified"),
                    "filename": filename,
                    "present": local_present if filename else None,
                    "url": entry.get("url"),
                }
            )

        if options["json"]:
            self.stdout.write(json.dumps(rows, indent=2, ensure_ascii=False))
            return

        self.stdout.write(f"Config: {CONFIG_PATH}")
        self.stdout.write(f"Data dir: {DATA_DIR}\n")
        header = f"{'KEY':<22} {'KIND':<9} {'VERSION':<18} {'FETCHED':<12} {'LOCAL'}"
        self.stdout.write(header)
        self.stdout.write("-" * len(header))
        for r in rows:
            if r["present"] is None:
                local = "-"
            else:
                local = "present" if r["present"] else "MISSING"
            self.stdout.write(
                f"{r['key']:<22} {r['kind']:<9} {str(r['version'] or '-'):<18} "
                f"{str(r['fetched'] or '-'):<12} {local}"
            )
