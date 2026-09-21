from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0018_openaiusage"),
    ]

    operations = [
        migrations.AddField(
            model_name="openaiusage",
            name="retry_reason",
            field=models.CharField(blank=True, max_length=100),
        ),
    ]
