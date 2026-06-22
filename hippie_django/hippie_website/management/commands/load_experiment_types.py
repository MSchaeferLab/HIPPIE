import csv
from datetime import datetime
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
        parser.add_argument(
            "--on-conflict",
            choices=["keep", "change", "discard"],
            help="Conflict behavior for PSI-MI/name mismatches. If set, command is non-interactive and logs conflicts.",
        )
     

    def handle(self, **options) -> None:
        path = Path(options["csv_path"])
        on_conflict = options.get("on_conflict")
        if not path.exists():
            raise CommandError(f"File not found: {path}")

        created = updated = skipped = 0
        rename_dict = dict()
        conflict_log_lines: list[str] = []
        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh, delimiter="\t")
            next(reader) #header

            for line_number, row in enumerate(reader, start=2):
                psi_mi_code = row[0].strip()
                name = row[1].strip()
                new_score = row[2].strip()

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
                        conflict_msg = (
                            f"line={line_number}\tpsi_mi={psi_mi_code}\texisting_name={obj.name}\tincoming_name={name}"
                        )
                        if obj.name in rename_dict:
                            new_name = rename_dict[obj.name]
                            conflict_log_lines.append(f"{conflict_msg}\taction=rename_cache\tresult={new_name}")
                        else:
                            if on_conflict:
                                confirm = on_conflict
                            else:
                                confirm = input(
                                    f"PSI-MI code {psi_mi_code} already exists with name {obj.name!r}. Keep old, change to new or discard? to {name!r}? [k/ch/di]"
                                )
                            if confirm.lower() in {"di", "discard"}:
                                self.stdout.write(f"Skipping {psi_mi_code} ({name})")
                                skipped += 1
                                conflict_log_lines.append(f"{conflict_msg}\taction=discard")
                                continue
                            elif confirm.lower() in {"k", "keep"}:
                                rename_dict[obj.name] = obj.name
                                new_name = obj.name
                                conflict_log_lines.append(f"{conflict_msg}\taction=keep")
                    
                            elif confirm.lower() in {"ch", "change"}:
                                check_if_name_exists(name)
                                rename_dict[obj.name] = name 
                                new_name = name
                                conflict_log_lines.append(f"{conflict_msg}\taction=change")
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

        if on_conflict and conflict_log_lines:
            log_dir = Path(__file__).resolve().parents[3] / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            conflict_log_path = log_dir / f"load_experiment_types_conflicts_{datetime.now().date()}.log"
            with conflict_log_path.open("a", encoding="utf-8") as log_file:
                log_file.write("\n".join(conflict_log_lines) + "\n")
            self.stdout.write(f"Conflict log written to {conflict_log_path}")

        self.stdout.write(
            self.style.SUCCESS(
                f"Done — created: {created}, updated: {updated}, skipped: {skipped}"
            )
        )
