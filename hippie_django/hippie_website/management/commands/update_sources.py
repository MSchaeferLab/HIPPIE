"""
update_sources — write ``data/sources_db.json`` into the ``Source`` table.

Overwrites ``Source.url`` and ``Source.description`` for every existing source that
has a registry entry (admin edits to those two fields are replaced). Sources in the
DB without an entry are left alone and listed; registry entries without a DB row
are ignored (nothing is created).

Usage:
    python manage.py update_sources
    python manage.py update_sources --file path/to/sources_db.json
"""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError, CommandParser

from hippie_website.models import Source
from hippie_website.source_links import _key, load_registry, registry_path


def sync_sources(path: Path | None = None) -> tuple[int, list[str]]:
    """Overwrite url/description from the registry; return (updated, unknown names)."""
    registry = load_registry(path)
    changed: list[Source] = []
    unknown: list[str] = []
    for source in Source.objects.all():
        entry = registry.get(_key(source.name))
        if entry is None:
            unknown.append(source.name)
            continue
        url = entry.get("url", "")
        description = entry.get("description", "")
        if source.url != url or source.description != description:
            source.url = url
            source.description = description
            changed.append(source)
    if changed:
        Source.objects.bulk_update(changed, ["url", "description"], batch_size=1_000)
    return len(changed), sorted(unknown)


class Command(BaseCommand):
    help = "Overwrite Source.url and Source.description from data/sources_db.json."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--file",
            type=Path,
            default=None,
            help="Registry JSON (default: data/sources_db.json).",
        )

    def handle(self, *args: object, **options: Path | None) -> None:
        path = options["file"] or registry_path()
        if not path.exists():
            raise CommandError(f"Source registry not found: {path}")
        updated, unknown = sync_sources(path)
        self.stdout.write(f"Updated {updated} source(s) from {path}.")
        if unknown:
            self.stderr.write(
                "No registry entry for: " + ", ".join(unknown) + " (left unchanged)."
            )
