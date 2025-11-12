from django.db import migrations, models


def migrate_dataset_files_to_user_files(apps, schema_editor):
    Dataset = apps.get_model('api', 'Dataset')
    UserFile = apps.get_model('api', 'UserFile')

    for dataset in Dataset.objects.all():
        dataset_file = getattr(dataset, 'file', None)
        if not dataset_file:
            continue
        file_name = getattr(dataset_file, 'name', None)
        if not file_name:
            continue
        UserFile.objects.create(
            dataset_id=dataset.id,
            file=file_name,
        )


def migrate_user_files_back_to_dataset(apps, schema_editor):
    Dataset = apps.get_model('api', 'Dataset')
    UserFile = apps.get_model('api', 'UserFile')

    for dataset in Dataset.objects.all():
        user_file = (
            UserFile.objects.filter(dataset_id=dataset.id)
            .order_by('uploaded_at', 'id')
            .first()
        )
        if not user_file or not user_file.file:
            continue
        dataset.file = user_file.file.name
        dataset.save(update_fields=['file'])


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0003_alter_agent_task_alter_dataset_description_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='UserFile',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('uploaded_at', models.DateTimeField(auto_now_add=True)),
                ('file', models.FileField(upload_to='user_files')),
                ('dataset', models.ForeignKey(on_delete=models.CASCADE, related_name='user_files', to='api.dataset')),
            ],
            options={
                'ordering': ['uploaded_at', 'id'],
            },
        ),
        migrations.RunPython(
            migrate_dataset_files_to_user_files,
            migrate_user_files_back_to_dataset,
        ),
        migrations.RemoveField(
            model_name='dataset',
            name='file',
        ),
    ]

