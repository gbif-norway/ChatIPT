# Generated manually for direct PDF file attachment caching.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0006_dataset_source_mode_pdfextraction'),
    ]

    operations = [
        migrations.AddField(
            model_name='userfile',
            name='openai_file_fingerprint',
            field=models.CharField(blank=True, max_length=128),
        ),
        migrations.AddField(
            model_name='userfile',
            name='openai_file_id',
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.DeleteModel(
            name='PdfExtraction',
        ),
    ]
