from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0036_dwcconversion_drop_unlinked_extension_rows'),
    ]

    operations = [
        migrations.AddField(
            model_name='dwcconversion',
            name='tidy',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
