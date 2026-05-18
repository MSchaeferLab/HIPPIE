import csv
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from hippie_website.models import ExperimentType

def check_if_name_exists(name):
    if ExperimentType.objects.filter(name=name).exists():
        obj = ExperimentType.objects.get(name=name)
        print(f"Name {name!r} already exists with PSI-MI code {obj.psi_mi_code!r}.")
        raise CommandError(f"Name {name!r} already exists with PSI-MI code {obj.psi_mi_code!r}.")
    return False

class Command(BaseCommand):
    help = "Update or create ExperimentType rows from a TSV scoring file."

    def add_arguments(self, parser):
        parser.add_argument(
            "--csv_path",
            nargs="?",
            default=str(Path(__file__).resolve().parents[3] / "data" / "techniques_scoring_04-05-26.csv"),
            help="Path to the techniques scoring TSV (default: data/techniques_scoring_04-05-26.csv)",
        )
    

    def handle(self, **options) -> None:
        path = Path(options["csv_path"])
        if not path.exists():
            raise CommandError(f"File not found: {path}")

        created = updated = skipped = 0
        rename_dict = dict()
        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh, delimiter="\t")
            next(reader) #header

            for row in reader:
                psi_mi_code = row[0].strip()
                name = row[1].strip()
                new_score = row[4].strip()

                try:
                    quality_score = float(new_score)
                except ValueError:
                    self.stderr.write(f"Line {name}:{psi_mi_code}: invalid score {new_score!r}, skipping")
                    skipped += 1
                    continue

                if psi_mi_code == "":
                    obj, was_created = ExperimentType.objects.get_or_create(
                        name=name)
                else:
                    obj, was_created = ExperimentType.objects.get_or_create(
                        psi_mi_code=psi_mi_code, defaults={"name": name})
                    if not was_created and obj.name != name:
                        if obj.name in rename_dict:
                            new_name = rename_dict[obj.name]
                        else:
                            confirm = input(f"PSI-MI code {psi_mi_code} already exists with name {obj.name!r}. Keep old, change to new or discard? to {name!r}? [k/ch/di]")
                            if confirm.lower() == "di":
                                self.stdout.write(f"Skipping {psi_mi_code} ({name})")
                                skipped += 1
                                continue
                            elif confirm.lower() == "k":
                                print("k")
                                rename_dict[obj.name] = obj.name
                                new_name = obj.name
                    
                            elif confirm.lower() == "ch":
                                print("ch")
                                check_if_name_exists(name)
                                rename_dict[obj.name] = name 
                                new_name = name
                            else:
                                raise CommandError("Invalid input, expected 'k', 'ch' or 'di'.")
                        obj.name = new_name
                        print(f"saving: {obj.name} \t {obj.psi_mi_code}")
                        obj.save()
                        
                if obj.quality_score == quality_score:
                    continue
                
                else:
                    obj.quality_score = quality_score
                    print(f"saving: {obj.name} \t {obj.psi_mi_code}")

                    obj.save()
                
                if was_created:
                    created += 1
                else:
                    updated += 1    

        self.stdout.write(
            self.style.SUCCESS(
                f"Done — created: {created}, updated: {updated}, skipped: {skipped}"
            )
        )
