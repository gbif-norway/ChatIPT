from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0012_dataset_dwc_dp_modeling_mode'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='dataset',
            name='dwc_dp_modeling_mode',
        ),
    ]
