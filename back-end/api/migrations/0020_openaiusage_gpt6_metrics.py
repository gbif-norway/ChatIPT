from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0019_openaiusage_retry_reason"),
    ]

    operations = [
        migrations.AddField(
            model_name="openaiusage",
            name="cache_write_price_per_million",
            field=models.DecimalField(
                blank=True,
                decimal_places=4,
                max_digits=12,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="openaiusage",
            name="duration_ms",
            field=models.PositiveBigIntegerField(default=0),
        ),
    ]
