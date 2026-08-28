from django.db import migrations, models
import pandas as pd


def display_columns(raw_columns):
    def display_label(raw_column):
        try:
            missing = bool(pd.isna(raw_column))
        except (TypeError, ValueError):
            missing = False
        label = '' if missing else str(raw_column)
        return label or 'Unnamed column'

    labels = []
    totals = {}
    for raw_column in raw_columns:
        label = display_label(raw_column)
        totals[label] = totals.get(label, 0) + 1

    seen = {}
    for raw_column in raw_columns:
        label = display_label(raw_column)
        seen[label] = seen.get(label, 0) + 1
        if totals[label] > 1:
            label = f"{label} ({seen[label]})"
        labels.append(label)
    return labels


def populate_table_shape_metadata(apps, schema_editor):
    Table = apps.get_model('api', 'Table')
    table_ids = Table.objects.order_by('id').values_list('id', flat=True).iterator(chunk_size=100)
    for table_id in table_ids:
        table = Table.objects.only('id', 'df').get(id=table_id)
        table.row_count = int(len(table.df.index))
        table.columns = display_columns(table.df.columns)
        table.save(update_fields=['row_count', 'columns'])


class Migration(migrations.Migration):
    dependencies = [
        ('api', '0014_remove_dataset_dwc_dp_accounting_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='table',
            name='columns',
            field=models.JSONField(default=list, editable=False),
        ),
        migrations.AddField(
            model_name='table',
            name='row_count',
            field=models.PositiveBigIntegerField(default=0, editable=False),
        ),
        migrations.RunPython(populate_table_shape_metadata, migrations.RunPython.noop),
    ]
