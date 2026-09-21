import logging
from decimal import Decimal

from django.db.models import Count, Sum


logger = logging.getLogger(__name__)

TOKENS_PER_MILLION = Decimal("1000000")
GPT_5_4_LONG_CONTEXT_THRESHOLD = 272_000
GPT_5_4_PRICING_SOURCE = "https://developers.openai.com/api/docs/models/gpt-5.4"

GPT_5_4_STANDARD_PRICING = {
    "input": Decimal("2.50"),
    "cached_input": Decimal("0.25"),
    "output": Decimal("15.00"),
}


def _value(obj, name, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _integer(obj, name):
    value = _value(obj, name, 0)
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _is_gpt_5_4(model):
    return model == "gpt-5.4" or model.startswith("gpt-5.4-")


def response_usage_defaults(response, requested_model="", reasoning_effort=""):
    """Return the auditable usage fields stored for one Responses API call."""
    usage = _value(response, "usage", {}) or {}
    input_details = _value(usage, "input_tokens_details", {}) or {}
    output_details = _value(usage, "output_tokens_details", {}) or {}

    input_tokens = _integer(usage, "input_tokens")
    cached_input_tokens = min(
        _integer(input_details, "cached_tokens"),
        input_tokens,
    )
    cache_write_input_tokens = min(
        _integer(input_details, "cache_write_tokens"),
        max(input_tokens - cached_input_tokens, 0),
    )
    output_tokens = _integer(usage, "output_tokens")
    reasoning_tokens = min(
        _integer(output_details, "reasoning_tokens"),
        output_tokens,
    )
    total_tokens = _integer(usage, "total_tokens") or input_tokens + output_tokens
    model = str(_value(response, "model", requested_model) or requested_model or "")
    service_tier = str(_value(response, "service_tier", "") or "")
    long_context = _is_gpt_5_4(model) and input_tokens > GPT_5_4_LONG_CONTEXT_THRESHOLD

    defaults = {
        "model": model,
        "reasoning_effort": reasoning_effort or "",
        "service_tier": service_tier,
        "response_status": str(_value(response, "status", "") or ""),
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "cache_write_input_tokens": cache_write_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": total_tokens,
        "long_context": long_context,
    }

    if not _is_gpt_5_4(model) or service_tier not in {"", "auto", "default"}:
        defaults.update({
            "input_price_per_million": None,
            "cached_input_price_per_million": None,
            "output_price_per_million": None,
            "input_price_multiplier": None,
            "output_price_multiplier": None,
            "estimated_cost_usd": None,
            "pricing_source": "",
        })
        return defaults

    input_multiplier = Decimal("2") if long_context else Decimal("1")
    output_multiplier = Decimal("1.5") if long_context else Decimal("1")
    uncached_input_tokens = max(
        input_tokens - cached_input_tokens - cache_write_input_tokens,
        0,
    )
    input_cost = (
        Decimal(uncached_input_tokens + cache_write_input_tokens)
        * GPT_5_4_STANDARD_PRICING["input"]
        + Decimal(cached_input_tokens) * GPT_5_4_STANDARD_PRICING["cached_input"]
    ) * input_multiplier / TOKENS_PER_MILLION
    output_cost = (
        Decimal(output_tokens)
        * GPT_5_4_STANDARD_PRICING["output"]
        * output_multiplier
        / TOKENS_PER_MILLION
    )

    defaults.update({
        "input_price_per_million": GPT_5_4_STANDARD_PRICING["input"],
        "cached_input_price_per_million": GPT_5_4_STANDARD_PRICING["cached_input"],
        "output_price_per_million": GPT_5_4_STANDARD_PRICING["output"],
        "input_price_multiplier": input_multiplier,
        "output_price_multiplier": output_multiplier,
        "estimated_cost_usd": (input_cost + output_cost).quantize(Decimal("0.000001")),
        "pricing_source": GPT_5_4_PRICING_SOURCE,
    })
    return defaults


def record_response_usage(
    response,
    agent_id,
    requested_model="",
    reasoning_effort="",
    retry_reason="",
):
    """Persist one response exactly once, keyed by the OpenAI response id."""
    from api.models import Agent, OpenAIUsage

    response_id = str(_value(response, "id", "") or "")
    if not response_id:
        logger.warning("Cannot record OpenAI usage because the response has no id")
        return None

    agent = Agent.objects.select_related("dataset", "task").get(pk=agent_id)
    defaults = response_usage_defaults(
        response,
        requested_model=requested_model,
        reasoning_effort=reasoning_effort,
    )
    defaults.update({
        "dataset": agent.dataset,
        "agent": agent,
        "task_name": agent.task.name,
        "retry_reason": retry_reason or "",
    })
    record, _ = OpenAIUsage.objects.update_or_create(
        response_id=response_id,
        defaults=defaults,
    )
    return record


def usage_summary(queryset):
    totals = queryset.aggregate(
        recorded_calls=Count("id"),
        input_tokens=Sum("input_tokens"),
        cached_input_tokens=Sum("cached_input_tokens"),
        cache_write_input_tokens=Sum("cache_write_input_tokens"),
        output_tokens=Sum("output_tokens"),
        reasoning_tokens=Sum("reasoning_tokens"),
        total_tokens=Sum("total_tokens"),
        estimated_cost_usd=Sum("estimated_cost_usd"),
    )
    totals["long_context_calls"] = queryset.filter(long_context=True).count()
    totals["unpriced_calls"] = queryset.filter(estimated_cost_usd__isnull=True).count()
    for key, value in totals.items():
        if value is None:
            totals[key] = "0.000000" if key == "estimated_cost_usd" else 0
        elif isinstance(value, Decimal):
            totals[key] = f"{value:.6f}"
    return totals
