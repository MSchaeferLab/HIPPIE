"""
Django management command: clear_database

Run with: python manage.py clear_database

Deletes all application data from every model table.
After running, `python manage.py migrate` will recreate the schema.
Requires typing a confirmation phrase to prevent accidental execution.
"""

import sys

from django.core.management.base import BaseCommand
from django.db import transaction

CONFIRMATION_PHRASE = "yes, delete everything"


class Command(BaseCommand):
    help = "Delete all data from every application model table."

    def add_arguments(self, parser):
        parser.add_argument(
            "--yes",
            action="store_true",
            help=f'Skip interactive prompt (equivalent to typing "{CONFIRMATION_PHRASE}").',
        )

    def handle(self, *args, **options) -> None:
        from hippie_website.models import (
            BaitPreyAssociation,
            BaitPreyTest,
            ExperimentType,
            GOSlimTerm,
            Gene,
            GeneSynonym,
            Interaction,
            InteractionCrossReference,
            InteractionType,
            Isoform,
            MeSHTerm,
            NonInteraction,
            OrthologInteraction,
            Protein,
            ProteinTissue,
            Publication,
            SignalingEndpoint,
            Source,
            Species,
            Tissue,
        )

        if not options["yes"]:
            self.stdout.write(
                self.style.WARNING(
                    "\nThis will permanently delete ALL data from every model table.\n"
                    "The database schema is preserved; run `migrate` to reseed.\n"
                )
            )
            answer = input(f'Type exactly "{CONFIRMATION_PHRASE}" to proceed: ')
            if answer != CONFIRMATION_PHRASE:
                self.stdout.write(
                    self.style.ERROR("Aborted — confirmation phrase did not match.")
                )
                sys.exit(1)

        deletion_order = [
            BaitPreyAssociation,
            BaitPreyTest,
            NonInteraction,
            InteractionCrossReference,
            OrthologInteraction,
            Interaction,
            SignalingEndpoint,
            ProteinTissue,
            Isoform,
            Protein,
            GeneSynonym,
            Gene,
            Tissue,
            Source,
            ExperimentType,
            InteractionType,
            Species,
            GOSlimTerm,
            MeSHTerm,
            Publication,
        ]

        self.stdout.write("Clearing database…")
        with transaction.atomic():
            for model in deletion_order:
                count, _ = model.objects.all().delete()
                self.stdout.write(f"  {model.__name__}: deleted {count} row(s)")

        self.stdout.write(self.style.SUCCESS("Database cleared."))
