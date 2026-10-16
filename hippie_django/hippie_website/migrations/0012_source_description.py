# Short human-readable description per source ("NAME — description" on the
# information page). Filled by `manage.py update_sources` (run by `hippie_update`)
# from data/sources_db.json; editable in the admin until the next sync.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("hippie_website", "0011_interaction_alphafold_model_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="source",
            name="description",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]
