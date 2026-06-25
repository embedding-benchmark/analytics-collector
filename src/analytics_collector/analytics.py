from collections import Counter, defaultdict
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from urllib.parse import unquote, urlparse

from analytics_collector.repository import EventRepository


AUTO_DAILY_THRESHOLD_DAYS = 7
FUNNEL_NAME = "engagement"
FUNNEL_STEPS = [
    ("page_view", {"page_view"}),
    ("search_or_filter", {"search_changed", "filter_changed"}),
    ("compare_opened", {"compare_opened"}),
    ("download_or_external_click", {"csv_downloaded", "external_link_clicked"}),
]
RETENTION_WINDOWS = (1, 7, 30)
DOMAIN_FIELDS = {
    "benchmarks": ("benchmarkMetrics", "benchmark"),
    "models": ("modelMetrics", "model"),
    "searches": ("searchMetrics", "query"),
    "filters": ("filterMetrics", "filter"),
    "compares": ("compareMetrics", "comparison"),
    "tasks": ("taskMetrics", "task"),
}
FILTER_SEPARATOR = "\x1f"
ENTITY_ROUTE_SEGMENTS = {
    "benchmarks": {"benchmark", "benchmarks"},
    "models": {"model", "models"},
    "tasks": {"task", "tasks"},
}


def day_start(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=UTC)


def day_after(value: date) -> datetime:
    return day_start(value) + timedelta(days=1)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def date_key(value: datetime) -> str:
    return ensure_utc(value).date().isoformat()


def hour_key(value: datetime) -> datetime:
    value = ensure_utc(value)
    return value.replace(minute=0, second=0, microsecond=0)


def format_hour(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def metric_count(metric: dict[str, Any], field: str) -> int:
    return int(metric.get(field, 0))


async def aggregate_range(repository: EventRepository, start_date: date, end_date: date) -> dict[str, Any]:
    validate_range(start_date, end_date)
    start = day_start(start_date)
    end = day_after(end_date)
    events = await repository.fetch_events(start, end)
    all_events = await repository.fetch_events(None, None)

    hourly = build_bucket_metrics(events, "hour", all_events)
    daily = build_bucket_metrics(events, "day", all_events)
    funnels = build_funnel_metrics(events)
    retention = build_retention_metrics(all_events, start_date, end_date)

    await repository.replace_hourly_metrics(start_date, end_date, hourly)
    await repository.replace_daily_metrics(start_date, end_date, daily)
    await repository.replace_funnel_metrics(start_date, end_date, funnels)
    await repository.replace_retention_metrics(start_date, end_date, retention)

    return {"status": "ok", "rawEvents": len(events)}


async def summary(repository: EventRepository, start_date: date, end_date: date) -> dict[str, Any]:
    validate_range(start_date, end_date)
    granularity = choose_granularity(start_date, end_date)
    metrics = (
        await repository.list_hourly_metrics(start_date, end_date)
        if granularity == "hour"
        else await repository.list_daily_metrics(start_date, end_date)
    )
    series = [public_bucket_metric(metric, granularity) for metric in metrics]
    totals = total_metrics(metrics)
    return {
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "granularity": granularity,
        "totals": totals,
        "series": series,
    }


async def distribution(repository: EventRepository, start_date: date, end_date: date, kind: str) -> dict[str, Any]:
    validate_range(start_date, end_date)
    metrics = await repository.list_daily_metrics(start_date, end_date)
    field = {"events": "eventCounts", "pages": "pageCounts", "countries": "countryCounts"}[kind]
    key_name = {"events": "eventName", "pages": "path", "countries": "country"}[kind]
    counts: Counter[str] = Counter()
    for metric in metrics:
        counts.update(metric.get(field, {}))
    items = [{key_name: key, "count": count} for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]
    return {"startDate": start_date.isoformat(), "endDate": end_date.isoformat(), "items": items}


async def domain_distribution(repository: EventRepository, start_date: date, end_date: date, kind: str) -> dict[str, Any]:
    validate_range(start_date, end_date)
    metrics = await repository.list_daily_metrics(start_date, end_date)
    field, key_name = DOMAIN_FIELDS[kind]
    items = domain_items(metrics, field, key_name, kind)
    return {"startDate": start_date.isoformat(), "endDate": end_date.isoformat(), "items": items}


async def compare_distribution(repository: EventRepository, start_date: date, end_date: date) -> dict[str, Any]:
    validate_range(start_date, end_date)
    metrics = await repository.list_daily_metrics(start_date, end_date)
    return {
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "items": domain_items(metrics, "compareMetrics", "comparison", "compares"),
        "benchmarks": domain_items(metrics, "compareBenchmarkMetrics", "benchmark", "benchmarks"),
        "models": domain_items(metrics, "compareModelMetrics", "model", "models"),
    }


def domain_items(metrics: list[dict], field: str, key_name: str, kind: str) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = defaultdict(lambda: {"events": 0, "sessionIds": set(), "visitorIds": set()})
    for metric in metrics:
        for key, value in metric.get(field, {}).items():
            grouped[key]["events"] += int(value.get("events", 0))
            grouped[key]["sessionIds"].update(value.get("sessionIds", []))
            grouped[key]["visitorIds"].update(value.get("visitorIds", []))

    items = []
    for key, value in grouped.items():
        item = {
            "sessions": len(value["sessionIds"]),
            "events": value["events"],
            "visitors": len(value["visitorIds"]),
        }
        if kind == "filters":
            filter_key, filter_value = parse_filter_metric_key(key)
            item.update({"filterKey": filter_key, "filterValue": filter_value})
        else:
            item[key_name] = key
        items.append(item)

    return sorted(items, key=lambda item: (-item["sessions"], -item["events"], domain_sort_key(item, kind)))


async def funnels(repository: EventRepository, start_date: date, end_date: date) -> dict[str, Any]:
    validate_range(start_date, end_date)
    metrics = await repository.list_funnel_metrics(start_date, end_date)
    sessions_by_step = Counter()
    for metric in metrics:
        for step in metric.get("steps", []):
            sessions_by_step[step["step"]] += step["sessions"]
    base = sessions_by_step.get(FUNNEL_STEPS[0][0], 0)
    steps = [
        {
            "step": step_name,
            "sessions": sessions_by_step.get(step_name, 0),
            "conversionRate": rate(sessions_by_step.get(step_name, 0), base),
        }
        for step_name, _events in FUNNEL_STEPS
    ]
    return {"startDate": start_date.isoformat(), "endDate": end_date.isoformat(), "funnels": [{"name": FUNNEL_NAME, "steps": steps}]}


async def retention(repository: EventRepository, start_date: date, end_date: date) -> dict[str, Any]:
    validate_range(start_date, end_date)
    metrics = await repository.list_retention_metrics(start_date, end_date)
    return {
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "cohorts": [public_retention_metric(metric) for metric in metrics],
    }


def validate_range(start_date: date, end_date: date) -> None:
    if end_date < start_date:
        raise ValueError("endDate must be on or after startDate")


def choose_granularity(start_date: date, end_date: date) -> str:
    return "day" if (end_date - start_date).days > AUTO_DAILY_THRESHOLD_DAYS else "hour"


def build_bucket_metrics(events: list[dict], granularity: str, all_events: list[dict] | None = None) -> list[dict]:
    first_seen_by_visitor = first_seen_buckets(all_events or events, granularity)
    grouped: dict[Any, list[dict]] = defaultdict(list)
    for event in events:
        received_at = ensure_utc(event["receivedAt"])
        bucket = hour_key(received_at) if granularity == "hour" else received_at.date().isoformat()
        grouped[bucket].append(event)

    docs = []
    for bucket, bucket_events in grouped.items():
        visitors = {event["visitorId"] for event in bucket_events}
        sessions = {event["sessionId"] for event in bucket_events}
        new_visitors = {visitor for visitor in visitors if first_seen_by_visitor.get(visitor) == bucket}
        returning_visitors = visitors - new_visitors
        event_counts = Counter(event["eventName"] for event in bucket_events)
        page_counts = Counter(event.get("page", {}).get("path") for event in bucket_events if event.get("page", {}).get("path"))
        country_counts = Counter(event.get("geo", {}).get("country") for event in bucket_events if event.get("geo", {}).get("country"))
        domain_metrics = build_domain_metrics(bucket_events)
        doc = {
            "date": bucket.date().isoformat() if granularity == "hour" else bucket,
            "pageViews": event_counts.get("page_view", 0),
            "uniqueVisitors": len(visitors),
            "newVisitors": len(new_visitors),
            "returningVisitors": len(returning_visitors),
            "sessions": len(sessions),
            "events": len(bucket_events),
            "visitorIds": sorted(visitors),
            "sessionIds": sorted(sessions),
            "eventCounts": dict(event_counts),
            "pageCounts": dict(page_counts),
            "countryCounts": dict(country_counts),
            **domain_metrics,
        }
        if granularity == "hour":
            doc["bucket"] = bucket
        else:
            doc["bucket"] = bucket
        docs.append(doc)
    return sorted(docs, key=lambda metric: metric["bucket"])


def first_seen_buckets(events: list[dict], granularity: str) -> dict[str, Any]:
    first_seen: dict[str, datetime] = {}
    for event in events:
        visitor_id = event["visitorId"]
        received_at = ensure_utc(event["receivedAt"])
        if visitor_id not in first_seen or received_at < first_seen[visitor_id]:
            first_seen[visitor_id] = received_at
    return {
        visitor_id: hour_key(received_at) if granularity == "hour" else received_at.date().isoformat()
        for visitor_id, received_at in first_seen.items()
    }


def build_domain_metrics(events: list[dict]) -> dict[str, dict[str, dict[str, Any]]]:
    metrics = {
        "benchmarkMetrics": defaultdict(empty_domain_metric),
        "modelMetrics": defaultdict(empty_domain_metric),
        "searchMetrics": defaultdict(empty_domain_metric),
        "filterMetrics": defaultdict(empty_domain_metric),
        "compareMetrics": defaultdict(empty_domain_metric),
        "compareBenchmarkMetrics": defaultdict(empty_domain_metric),
        "compareModelMetrics": defaultdict(empty_domain_metric),
        "taskMetrics": defaultdict(empty_domain_metric),
    }
    for event in events:
        add_values(metrics["benchmarkMetrics"], extract_benchmarks(event), event)
        add_values(metrics["modelMetrics"], extract_models(event), event)
        add_values(metrics["searchMetrics"], extract_searches(event), event)
        add_values(metrics["filterMetrics"], extract_filters(event), event)
        add_values(metrics["compareMetrics"], extract_compares(event), event)
        add_values(metrics["compareBenchmarkMetrics"], extract_compare_benchmarks(event), event)
        add_values(metrics["compareModelMetrics"], extract_compare_models(event), event)
        add_values(metrics["taskMetrics"], extract_tasks(event), event)
    return {name: public_domain_metrics(values) for name, values in metrics.items()}


def empty_domain_metric() -> dict[str, Any]:
    return {"events": 0, "sessionIds": set(), "visitorIds": set(), "pathCounts": Counter(), "titleCounts": Counter()}


def add_values(metrics: dict[str, dict[str, Any]], values: list[str], event: dict) -> None:
    for value in values:
        metric = metrics[value]
        metric["events"] += 1
        metric["sessionIds"].add(event["sessionId"])
        metric["visitorIds"].add(event["visitorId"])
        add_domain_context(metric, event)


def add_domain_context(metric: dict[str, Any], event: dict) -> None:
    data = payload(event)
    path = clean_string(data.get("path")) or clean_string(event.get("page", {}).get("path"))
    title = clean_string(data.get("title"))
    if path:
        metric["pathCounts"][path] += 1
    if title:
        metric["titleCounts"][title] += 1


def public_domain_metrics(metrics: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        key: {
            "events": value["events"],
            "sessionIds": sorted(value["sessionIds"]),
            "visitorIds": sorted(value["visitorIds"]),
            "pathCounts": dict(sorted(value["pathCounts"].items(), key=lambda item: (-item[1], item[0]))),
            "titleCounts": dict(sorted(value["titleCounts"].items(), key=lambda item: (-item[1], item[0]))),
        }
        for key, value in metrics.items()
    }


def payload(event: dict) -> dict[str, Any]:
    value = event.get("payload")
    return value if isinstance(value, dict) else {}


def clean_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def clean_strings(value: Any) -> list[str]:
    if isinstance(value, list):
        return [cleaned for item in value if (cleaned := clean_string(item))]
    cleaned = clean_string(value)
    return [cleaned] if cleaned else []


def extract_benchmarks(event: dict) -> list[str]:
    if event["eventName"] in {"compare_opened", "compare_model_changed"}:
        return []
    data = payload(event)
    return unique_values(
        [
            *clean_strings(data.get("benchmarkId")),
            *clean_strings(data.get("benchmarkName")),
            *extract_route_entities(event, "benchmarks"),
        ]
    )


def extract_models(event: dict) -> list[str]:
    if event["eventName"] in {"compare_opened", "compare_model_changed"}:
        return []
    data = payload(event)
    values = [
        *clean_strings(data.get("modelId")),
        *clean_strings(data.get("modelName")),
        *clean_strings(data.get("modelIds")),
        *clean_strings(data.get("modelNames")),
        *clean_strings(data.get("models")),
        *extract_route_entities(event, "models"),
    ]
    return unique_values(values)


def extract_searches(event: dict) -> list[str]:
    if event["eventName"] != "search_changed":
        return []
    return clean_strings(payload(event).get("query"))


def extract_filters(event: dict) -> list[str]:
    if event["eventName"] != "filter_changed":
        return []
    data = payload(event)
    filter_key = clean_string(data.get("filterKey"))
    filter_value = clean_string(data.get("filterValue"))
    if not filter_key or not filter_value:
        return []
    return [format_filter_metric_key(filter_key, filter_value)]


def extract_tasks(event: dict) -> list[str]:
    data = payload(event)
    values = [
        *clean_strings(data.get("task")),
        *clean_strings(data.get("taskId")),
        *clean_strings(data.get("taskName")),
        *extract_route_entities(event, "tasks"),
    ]
    return unique_values(values)


def extract_route_entities(event: dict, kind: str) -> list[str]:
    data = payload(event)
    path = clean_string(data.get("path")) or clean_string(event.get("page", {}).get("path"))
    if not path or not route_matches_entity_kind(path, kind):
        return []
    title = clean_string(data.get("title"))
    if title:
        return [title]
    slug = route_entity_slug(path, kind)
    return [slug] if slug else []


def route_matches_entity_kind(path: str, kind: str) -> bool:
    return route_entity_slug(path, kind) is not None


def route_entity_slug(path: str, kind: str) -> str | None:
    segments = route_segments(path)
    aliases = ENTITY_ROUTE_SEGMENTS[kind]
    for index, segment in enumerate(segments[:-1]):
        if segment.lower() in aliases:
            return segments[index + 1]
    return None


def route_segments(path: str) -> list[str]:
    parsed_path = urlparse(path).path
    return [unquote(segment).strip() for segment in parsed_path.split("/") if segment.strip()]


def extract_compares(event: dict) -> list[str]:
    if event["eventName"] not in {"compare_opened", "compare_model_changed"}:
        return []
    models = extract_compare_models(event)
    if len(models) < 2:
        return []
    return [" vs ".join(sorted(models[:2]))]


def extract_compare_benchmarks(event: dict) -> list[str]:
    if event["eventName"] not in {"compare_opened", "compare_model_changed"}:
        return []
    data = payload(event)
    return unique_values(
        [
            *clean_strings(data.get("benchmark")),
            *clean_strings(data.get("benchmarkId")),
            *clean_strings(data.get("benchmarkName")),
        ]
    )


def extract_compare_models(event: dict) -> list[str]:
    if event["eventName"] not in {"compare_opened", "compare_model_changed"}:
        return []
    data = payload(event)
    return unique_values(
        [
            *clean_strings(data.get("model")),
            *clean_strings(data.get("modelId")),
            *clean_strings(data.get("modelName")),
            *clean_strings(data.get("modelIds")),
            *clean_strings(data.get("modelNames")),
            *clean_strings(data.get("models")),
        ]
    )


def unique_values(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def format_filter_metric_key(filter_key: str, filter_value: str) -> str:
    return f"{filter_key}{FILTER_SEPARATOR}{filter_value}"


def parse_filter_metric_key(value: str) -> tuple[str, str]:
    if FILTER_SEPARATOR not in value:
        return value, ""
    filter_key, filter_value = value.split(FILTER_SEPARATOR, 1)
    return filter_key, filter_value


def domain_sort_key(item: dict[str, Any], kind: str) -> str:
    if kind == "filters":
        return f"{item.get('filterKey', '')}={item.get('filterValue', '')}"
    return str(item.get(DOMAIN_FIELDS[kind][1], ""))


def build_funnel_metrics(events: list[dict]) -> list[dict]:
    sessions_by_date: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for event in events:
        sessions_by_date[date_key(event["receivedAt"])][event["sessionId"]].append(event)

    docs = []
    for metric_date, sessions in sessions_by_date.items():
        step_counts = Counter()
        for session_events in sessions.values():
            completed = completed_funnel_steps(session_events)
            for step_name in completed:
                step_counts[step_name] += 1
        base = step_counts.get(FUNNEL_STEPS[0][0], 0)
        docs.append(
            {
                "date": metric_date,
                "name": FUNNEL_NAME,
                "steps": [
                    {"step": step_name, "sessions": step_counts.get(step_name, 0), "conversionRate": rate(step_counts.get(step_name, 0), base)}
                    for step_name, _events in FUNNEL_STEPS
                ],
            }
        )
    return sorted(docs, key=lambda metric: metric["date"])


def completed_funnel_steps(events: list[dict]) -> list[str]:
    ordered = sorted(events, key=lambda event: ensure_utc(event["receivedAt"]))
    completed: list[str] = []
    step_index = 0
    for event in ordered:
        if event["eventName"] in FUNNEL_STEPS[step_index][1]:
            completed.append(FUNNEL_STEPS[step_index][0])
            step_index += 1
            if step_index == len(FUNNEL_STEPS):
                break
    return completed


def build_retention_metrics(events: list[dict], start_date: date, end_date: date) -> list[dict]:
    active_dates_by_visitor: dict[str, set[date]] = defaultdict(set)
    for event in events:
        active_dates_by_visitor[event["visitorId"]].add(ensure_utc(event["receivedAt"]).date())

    cohorts: dict[date, list[set[date]]] = defaultdict(list)
    for dates in active_dates_by_visitor.values():
        first_seen = min(dates)
        if start_date <= first_seen <= end_date:
            cohorts[first_seen].append(dates)

    docs = []
    for cohort_date, visitors in cohorts.items():
        size = len(visitors)
        doc = {"cohortDate": cohort_date.isoformat(), "size": size}
        for window in RETENTION_WINDOWS:
            retained = sum(1 for dates in visitors if cohort_date + timedelta(days=window) in dates)
            doc[f"d{window}"] = retained
            doc[f"d{window}Rate"] = rate(retained, size)
        docs.append(doc)
    return sorted(docs, key=lambda metric: metric["cohortDate"])


def total_metrics(metrics: list[dict]) -> dict[str, int]:
    visitor_ids = set()
    session_ids = set()
    for metric in metrics:
        visitor_ids.update(metric.get("visitorIds", []))
        session_ids.update(metric.get("sessionIds", []))
    return {
        "pageViews": sum(metric_count(metric, "pageViews") for metric in metrics),
        "uniqueVisitors": len(visitor_ids),
        "newVisitors": sum(metric_count(metric, "newVisitors") for metric in metrics),
        "returningVisitors": sum(metric_count(metric, "returningVisitors") for metric in metrics),
        "sessions": len(session_ids),
        "events": sum(metric_count(metric, "events") for metric in metrics),
    }


def public_bucket_metric(metric: dict, granularity: str) -> dict[str, Any]:
    bucket = format_hour(metric["bucket"]) if granularity == "hour" else metric["bucket"]
    return {
        "bucket": bucket,
        "pageViews": metric_count(metric, "pageViews"),
        "uniqueVisitors": metric_count(metric, "uniqueVisitors"),
        "newVisitors": metric_count(metric, "newVisitors"),
        "returningVisitors": metric_count(metric, "returningVisitors"),
        "sessions": metric_count(metric, "sessions"),
        "events": metric_count(metric, "events"),
    }


def public_retention_metric(metric: dict) -> dict[str, Any]:
    return {
        "cohortDate": metric["cohortDate"],
        "size": metric["size"],
        "d1": metric.get("d1", 0),
        "d1Rate": metric.get("d1Rate", 0),
        "d7": metric.get("d7", 0),
        "d7Rate": metric.get("d7Rate", 0),
        "d30": metric.get("d30", 0),
        "d30Rate": metric.get("d30Rate", 0),
    }


def rate(count: int, total: int) -> float:
    if total == 0:
        return 0.0
    return count / total
