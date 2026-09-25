from django.db import migrations, models
import django.utils.timezone

class Migration(migrations.Migration):
    dependencies = [
        ('api', '0022_agentturnjob'),
    ]

    operations = [
        migrations.AlterField(
            model_name='datasetattentionnotification',
            name='attention_kind',
            field=models.CharField(blank=True, choices=[('needs_input', 'Needs input'), ('ready', 'Package ready'), ('published', 'Published'), ('failed', 'Processing failed')], max_length=20),
        ),
        migrations.AddField(
            model_name='agentturnjob',
            name='available_at',
            field=models.DateTimeField(db_index=True, default=django.utils.timezone.now),
        ),
        migrations.AddField(
            model_name='agentturnjob',
            name='failure_count',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='datasetattentionnotification',
            name='ready_event_key',
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name='datasetattentionnotification',
            name='last_sent_event_key',
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name='datasetattentionnotification',
            name='ready_sent_key',
            field=models.CharField(blank=True, max_length=100),
        ),
    ]
