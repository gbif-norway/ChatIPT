import json
from datetime import datetime, time

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from api.models import OpenAIUsage
from api.openai_usage import usage_summary


def _start_time(value):
    parsed_datetime = parse_datetime(value)
    if parsed_datetime is not None:
        if timezone.is_naive(parsed_datetime):
            return timezone.make_aware(parsed_datetime)
        return parsed_datetime

    parsed_date = parse_date(value)
    if parsed_date is not None:
        return timezone.make_aware(datetime.combine(parsed_date, time.min))
    raise CommandError("--since must be an ISO date or datetime")


class Command(BaseCommand):
    help = "Report model quality proxies, latency, token use, retries, and cost."

    def add_arguments(self, parser):
        parser.add_argument("--since", help="Only include calls at or after this ISO date/datetime.")
        parser.add_argument("--dataset", type=int, help="Only include one dataset ID.")

    def handle(self, *args, **options):
        records = OpenAIUsage.objects.select_related("agent").all()
        if options.get("since"):
            records = records.filter(created_at__gte=_start_time(options["since"]))
        if options.get("dataset"):
            records = records.filter(dataset_id=options["dataset"])

        by_model = []
        for model in records.order_by().values_list("model", flat=True).distinct():
            by_model.append({
                "model": model,
                **usage_summary(records.filter(model=model)),
            })

        by_service_tier = [
            {"service_tier": tier, **usage_summary(records.filter(service_tier=tier))}
            for tier in records.order_by().values_list("service_tier", flat=True).distinct()
        ]

        by_task_and_model = []
        groups = records.order_by().values_list("task_name", "model").distinct()
        for task_name, model in groups:
            by_task_and_model.append({
                "task_name": task_name,
                "model": model,
                **usage_summary(records.filter(task_name=task_name, model=model)),
            })

        report = {
            "generated_at": timezone.now().isoformat(),
            "filters": {
                "since": options.get("since") or "",
                "dataset": options.get("dataset"),
            },
            "overall": usage_summary(records),
            "by_model": by_model,
            "by_service_tier": by_service_tier,
            "by_task_and_model": by_task_and_model,
        }
        self.stdout.write(json.dumps(report, indent=2, sort_keys=True))
