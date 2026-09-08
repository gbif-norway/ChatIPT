from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0016_dataset_dwca_validation"),
    ]

    operations = [
        migrations.AddField(
            model_name="userfile",
            name="source_manifest",
            field=models.JSONField(default=dict, editable=False),
        ),
    ]
