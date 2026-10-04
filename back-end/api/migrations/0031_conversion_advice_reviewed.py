from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('api', '0030_conversion_usage')]

    operations = [
        migrations.AddField(
            model_name='dwcconversion', name='advice_reviewed',
            field=models.JSONField(default=list),
        ),
    ]
