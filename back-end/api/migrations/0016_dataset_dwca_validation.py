from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('api', '0015_table_shape_metadata'),
    ]

    operations = [
        migrations.AddField(
            model_name='dataset',
            name='dwca_validation',
            field=models.JSONField(blank=True, null=True),
        ),
    ]
