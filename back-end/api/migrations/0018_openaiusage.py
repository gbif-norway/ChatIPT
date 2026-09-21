from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0017_userfile_source_manifest"),
    ]

    operations = [
        migrations.CreateModel(
            name="OpenAIUsage",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("task_name", models.CharField(blank=True, max_length=300)),
                ("response_id", models.CharField(max_length=200, unique=True)),
                ("response_status", models.CharField(blank=True, max_length=40)),
                ("model", models.CharField(blank=True, max_length=100)),
                ("reasoning_effort", models.CharField(blank=True, max_length=30)),
                ("service_tier", models.CharField(blank=True, max_length=30)),
                ("input_tokens", models.PositiveBigIntegerField(default=0)),
                ("cached_input_tokens", models.PositiveBigIntegerField(default=0)),
                ("cache_write_input_tokens", models.PositiveBigIntegerField(default=0)),
                ("output_tokens", models.PositiveBigIntegerField(default=0)),
                ("reasoning_tokens", models.PositiveBigIntegerField(default=0)),
                ("total_tokens", models.PositiveBigIntegerField(default=0)),
                ("long_context", models.BooleanField(default=False)),
                ("input_price_per_million", models.DecimalField(blank=True, decimal_places=4, max_digits=12, null=True)),
                ("cached_input_price_per_million", models.DecimalField(blank=True, decimal_places=4, max_digits=12, null=True)),
                ("output_price_per_million", models.DecimalField(blank=True, decimal_places=4, max_digits=12, null=True)),
                ("input_price_multiplier", models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True)),
                ("output_price_multiplier", models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True)),
                ("estimated_cost_usd", models.DecimalField(blank=True, decimal_places=6, max_digits=16, null=True)),
                ("pricing_source", models.URLField(blank=True, max_length=500)),
                ("agent", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="openai_usage_records", to="api.agent")),
                ("dataset", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="openai_usage_records", to="api.dataset")),
            ],
            options={
                "ordering": ["created_at", "id"],
                "indexes": [
                    models.Index(fields=["dataset", "created_at"], name="api_openaiu_dataset_007bec_idx"),
                    models.Index(fields=["agent", "created_at"], name="api_openaiu_agent_i_d8e509_idx"),
                ],
            },
        ),
    ]
