from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0007_userfile_openai_file_cache'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='dataset',
            name='rejected_at',
        ),
    ]
