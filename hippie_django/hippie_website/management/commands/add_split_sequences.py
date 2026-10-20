"""
Add ``sequences.fasta`` to pre-computed split archives that lack it.

Split runs now ship the sequences of every protein in their CSVs (see
``nextflow_runner.package_results``). The pre-computed archives on the Download
page were cut before that; this appends the same subset to each of them in
place instead of re-running nine splits. Archives that already hold the file
are skipped, so it is safe to run twice.

    python manage.py add_split_sequences /path/to/downloads
    python manage.py add_split_sequences /path/to/downloads --pattern "HIPPIE-*.zip"
"""

import csv
import io
import tempfile
import zipfile
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError, CommandParser

from hippie_website.services.export_ppis import ARTIFACT_FILES, artifact_dir
from hippie_website.services.nextflow_runner import OUTPUT_FILES, write_sequences


def _split_proteins(zf: zipfile.ZipFile) -> set[str]:
    """Every accession in the archive's four split CSVs."""
    proteins: set[str] = set()
    for name in OUTPUT_FILES:
        with zf.open(name) as fh:
            for row in csv.DictReader(io.TextIOWrapper(fh)):
                proteins.update(row[k] for k in ("protein1", "protein2") if row[k])
    return proteins


class Command(BaseCommand):
    help = "Append sequences.fasta to pre-computed split archives lacking it."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("directory", help="Directory holding the split ZIPs")
        parser.add_argument(
            "--pattern",
            default="HIPPIE-*.zip",
            help="Glob for the archives (default: HIPPIE-*.zip)",
        )

    def handle(self, *_args: object, **options: object) -> None:
        directory = Path(str(options["directory"]))
        archives = sorted(directory.glob(str(options["pattern"])))
        if not archives:
            raise CommandError(
                f"No archives matching {options['pattern']} in {directory}"
            )
        source = artifact_dir() / ARTIFACT_FILES["sequences"]
        if not source.is_file():
            raise CommandError(f"Proteome FASTA not found: {source}")

        for path in archives:
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
                if "sequences.fasta" in names:
                    self.stdout.write(f"{path.name}: already has sequences.fasta")
                    continue
                missing = [n for n in OUTPUT_FILES if n not in names]
                if missing:
                    self.stdout.write(
                        self.style.WARNING(
                            f"{path.name}: skipped, no {', '.join(missing)} at the root"
                        )
                    )
                    continue
                proteins = _split_proteins(zf)
            with tempfile.TemporaryDirectory() as td:
                fasta = Path(td) / "sequences.fasta"
                written = write_sequences(fasta, source, proteins)
                with zipfile.ZipFile(path, "a", zipfile.ZIP_DEFLATED) as zf:
                    zf.write(fasta, "sequences.fasta")
            line = f"{path.name}: {written} sequences for {len(proteins)} proteins"
            if written < len(proteins):
                self.stdout.write(self.style.WARNING(line))
            else:
                self.stdout.write(self.style.SUCCESS(line))
