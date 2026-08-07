from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0011_datasetattentionnotification'),
    ]

    operations = [
        migrations.AddField(
            model_name='dataset',
            name='dwc_dp_modeling_mode',
            field=models.CharField(
                choices=[
                    ('guided', 'Guided package'),
                    ('rich', 'Rich relational package'),
                ],
                default='guided',
                max_length=20,
            ),
        ),
    ]
