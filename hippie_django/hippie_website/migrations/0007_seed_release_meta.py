"""Seed the initial ReleaseMeta row for the current HIPPIE release (v3.0).

Values are the documented v3.0 thresholds (medium = 0.63, high = 0.72) and the
resource versions known at build time. A real ``hippie_update`` /
``import_pod_data`` run overwrites the quartiles and merges live resource
versions into this row. Reversible: drops the seeded row only.
"""

from datetime import date

from django.db import migrations

SEED = {
    "release_number": 3,
    "version_label": "3.0",
    "int_median": 0.63,
    "int_q3": 0.72,
    "both_median": 0.63,
    "both_q3": 0.72,
    "resource_versions": {
        "BioGRID": "4.4",
        "IntAct": "current",
        "MINT": "current",
        "GTEx": "v8",
        "UniProt (idmapping)": "HUMAN_9606_idmapping.dat",
        "NCBI Gene": "Homo_sapiens.gene_info",
        "FlyBase": "fb_2026_01",
        "WormBase": "WS298",
        "Xenbase": "v1.2",
    },
}


def seed_release(apps, schema_editor):
    ReleaseMeta = apps.get_model("hippie_website", "ReleaseMeta")
    if ReleaseMeta.objects.exists():
        return
    ReleaseMeta.objects.create(release_date=date(2026, 6, 17), **SEED)


def unseed_release(apps, schema_editor):
    ReleaseMeta = apps.get_model("hippie_website", "ReleaseMeta")
    ReleaseMeta.objects.filter(release_number=3, version_label="3.0").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("hippie_website", "0006_releasemeta_protein_is_swissprot"),
    ]

    operations = [
        migrations.RunPython(seed_release, unseed_release),
    ]
