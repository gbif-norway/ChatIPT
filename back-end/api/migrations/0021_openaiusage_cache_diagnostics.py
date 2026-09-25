from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('api', '0020_openaiusage_gpt6_metrics'),
    ]

    operations = [
        migrations.AddField(
            model_name='openaiusage',
            name='cache_prefix_hash',
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name='openaiusage',
            name='cache_diagnostics',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
