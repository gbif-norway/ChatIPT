from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0008_remove_dataset_rejected_at"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=(
                        "ALTER TABLE api_dataset "
                        "ADD COLUMN IF NOT EXISTS dwc_dp_url varchar(2000) NOT NULL DEFAULT ''; "
                        "ALTER TABLE api_dataset ALTER COLUMN dwc_dp_url DROP DEFAULT; "
                        "ALTER TABLE api_dataset "
                        "ADD COLUMN IF NOT EXISTS dwc_dp_validation jsonb NULL;"
                    ),
                    reverse_sql=(
                        "ALTER TABLE api_dataset DROP COLUMN IF EXISTS dwc_dp_validation; "
                        "ALTER TABLE api_dataset DROP COLUMN IF EXISTS dwc_dp_url;"
                    ),
                ),
            ],
            state_operations=[
                migrations.AddField(
                    model_name="dataset",
                    name="dwc_dp_url",
                    field=models.CharField(blank=True, max_length=2000),
                ),
                migrations.AddField(
                    model_name="dataset",
                    name="dwc_dp_validation",
                    field=models.JSONField(blank=True, null=True),
                ),
            ],
        ),
    ]
