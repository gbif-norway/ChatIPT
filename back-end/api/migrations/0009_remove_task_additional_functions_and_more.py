# Generated by Django 5.1.1 on 2025-06-04 10:31

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0008_dataset_gbif_url'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='task',
            name='additional_functions',
        ),
        migrations.RemoveField(
            model_name='task',
            name='attempt_autonomous',
        ),
        migrations.RemoveField(
            model_name='task',
            name='per_table',
        ),
        migrations.AlterField(
            model_name='dataset',
            name='structure_notes',
            field=models.TextField(blank=True, default=''),
        ),
    ]
