from django.db import migrations, models


def set_initial_task_order(apps, schema_editor):
    """Set initial order based on current ID (load_tasks will update this properly)"""
    Task = apps.get_model('api', 'Task')
    for task in Task.objects.all():
        task.order = task.id
        task.save(update_fields=['order'])


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0004_userfile_multi_uploads'),
    ]

    operations = [
        migrations.AddField(
            model_name='task',
            name='order',
            field=models.IntegerField(default=0, help_text='Order in which tasks should be executed (from tasks.yaml)'),
        ),
        migrations.RunPython(set_initial_task_order, migrations.RunPython.noop),
        migrations.AlterModelOptions(
            name='task',
            options={'get_latest_by': 'id', 'ordering': ['order', 'id']},
        ),
    ]

