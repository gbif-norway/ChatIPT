from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('api', '0021_openaiusage_cache_diagnostics'),
    ]

    operations = [
        migrations.CreateModel(
            name='AgentTurnJob',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('requested_after_message_id', models.PositiveBigIntegerField()),
                ('requested_at', models.DateTimeField(auto_now_add=True)),
                ('claimed_at', models.DateTimeField(blank=True, db_index=True, null=True)),
                ('agent', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, to='api.agent')),
            ],
        ),
    ]
