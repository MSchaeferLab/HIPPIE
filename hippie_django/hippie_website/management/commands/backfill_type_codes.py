"""
Fill in missing ``InteractionType.psi_mi_code`` values from IntAct / BioGRID
MITAB files.

Imports before the fix in ``hippie_update`` kept only the interaction-type
name and discarded its PSI-MI code, so ``export_downloads`` could not write a
spec-compliant MITAB column 12. ``hippie_update`` now keeps the code and
backfills it on its next run; this command does the same without a full
update. It reads column 12 only and touches only rows whose code is empty.

    python manage.py backfill_type_codes data/intact.txt data/BIOGRID-ALL-5.0.259.mitab.txt
    python manage.py backfill_type_codes data/intact.txt --dry-run

Run ``export_downloads`` afterwards to regenerate the MITAB file.
"""

import contextlib

from django.core.management.base import BaseCommand, CommandParser

from hippie_website.models import InteractionType

from ._mitab import open_mitab
from .hippie_update import _extract_mi_code, _parse_interaction_type

# MITAB 2.5 column 12 (0-based 11): interaction type.
_TYPE_COLUMN = 11


def read_type_codes(paths: list[str]) -> dict[str, str]:
    """Map each interaction-type name in ``paths`` to its PSI-MI code."""
    codes: dict[str, str] = {}
    for path in paths:
        with contextlib.closing(open_mitab(path)) as fh:
            for line in fh:
                if len(line) <= _TYPE_COLUMN:
                    continue
                name = _parse_interaction_type(line[_TYPE_COLUMN])
                code = _extract_mi_code(line[_TYPE_COLUMN])
                if name and code:
                    codes.setdefault(name, code)
    return codes


class Command(BaseCommand):
    help = "Fill in missing InteractionType PSI-MI codes from MITAB files."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "files",
            nargs="+",
            metavar="FILE",
            help="IntAct / BioGRID MITAB file (path, .gz or URL)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing",
        )

    def handle(self, *_args: object, **options: object) -> None:
        paths: list[str] = options["files"]  # type: ignore[assignment]
        dry_run = bool(options["dry_run"])

        codes = read_type_codes(paths)
        self.stdout.write(f"Read {len(codes)} interaction-type codes.")

        missing = list(InteractionType.objects.filter(psi_mi_code=""))
        filled = [t for t in missing if t.name in codes]
        for itype in filled:
            itype.psi_mi_code = codes[itype.name]
            self.stdout.write(f"  {itype.psi_mi_code}  {itype.name}")
        if filled and not dry_run:
            InteractionType.objects.bulk_update(filled, ["psi_mi_code"])

        unmatched = sorted(t.name for t in missing if t.name not in codes)
        verb = "Would fill" if dry_run else "Filled"
        self.stdout.write(
            self.style.SUCCESS(f"{verb} {len(filled)} of {len(missing)} empty codes.")
        )
        if unmatched:
            self.stdout.write(
                self.style.WARNING(
                    f"No code found for {len(unmatched)}: {', '.join(unmatched)}"
                )
            )
