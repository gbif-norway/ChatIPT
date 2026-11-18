from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('api', '0042_dataset_dwca_row_types'),
    ]

    operations = [
        migrations.AddField(
            model_name='table',
            name='reviewed_taxon_ids',
            field=models.JSONField(default=dict, editable=False),
        ),
    ]
