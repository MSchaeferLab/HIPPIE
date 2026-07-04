from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("hippie_website", "0002_remove_redundant_features"),
    ]

    operations = [
        migrations.AddField(
            model_name="source",
            name="n_connected_interactions",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
