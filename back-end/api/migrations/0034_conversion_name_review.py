from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0033_conversion_review_chat'),
    ]

    operations = [
        migrations.AddField(
            model_name='dwcconversion',
            name='name_review',
            field=models.JSONField(default=dict),
        ),
    ]
