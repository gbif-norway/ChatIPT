from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0031_conversion_advice_reviewed'),
    ]

    operations = [
        migrations.AddField(
            model_name='dwcconversion',
            name='conflicts',
            field=models.JSONField(default=list),
        ),
        migrations.AddField(
            model_name='dwcconversion',
            name='retryable',
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name='dwcconversion',
            name='status',
            field=models.CharField(choices=[('queued', 'Queued'), ('inspecting', 'Inspecting'), ('review', 'Review'), ('reviewing', 'Reviewing'), ('converting', 'Converting'), ('complete', 'Complete'), ('blocked', 'Blocked'), ('failed', 'Failed')], default='queued', max_length=20),
        ),
    ]
