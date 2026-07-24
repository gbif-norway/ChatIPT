from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0009_dataset_dwc_dp_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="dataset",
            name="source_accounting_snapshot",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="dataset",
            name="dwc_dp_accounting",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
