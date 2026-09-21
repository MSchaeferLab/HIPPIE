# GTEx reports TPM, not RPKM, in the releases HIPPIE imports from
# (update_tissue_data.py). The field/verbose_name was still called RPKM from
# an earlier import that used an older GTEx format. Rename to match reality
# — no computation changes, the stored values are unaffected.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("hippie_website", "0008_splitjob_heartbeat_at"),
    ]

    operations = [
        migrations.RenameField(
            model_name="genetissue",
            old_name="median_rpkm",
            new_name="median_tpm",
        ),
        migrations.AlterField(
            model_name="genetissue",
            name="median_tpm",
            field=models.FloatField(verbose_name="tpm"),
        ),
    ]
