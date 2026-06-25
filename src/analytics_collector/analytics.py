from collections import Counter, defaultdict
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

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

    hourly = build_bucket_metrics(events, "hour")
    daily = build_bucket_metrics(events, "day")
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


def build_bucket_metrics(events: list[dict], granularity: str) -> list[dict]:
    grouped: dict[Any, list[dict]] = defaultdict(list)
    for event in events:
        received_at = ensure_utc(event["receivedAt"])
        bucket = hour_key(received_at) if granularity == "hour" else received_at.date().isoformat()
        grouped[bucket].append(event)

    docs = []
    for bucket, bucket_events in grouped.items():
        visitors = {event["visitorId"] for event in bucket_events}
        sessions = {event["sessionId"] for event in bucket_events}
        event_counts = Counter(event["eventName"] for event in bucket_events)
        page_counts = Counter(event.get("page", {}).get("path") for event in bucket_events if event.get("page", {}).get("path"))
        country_counts = Counter(event.get("geo", {}).get("country") for event in bucket_events if event.get("geo", {}).get("country"))
        doc = {
            "date": bucket.date().isoformat() if granularity == "hour" else bucket,
            "pageViews": event_counts.get("page_view", 0),
            "uniqueVisitors": len(visitors),
            "sessions": len(sessions),
            "events": len(bucket_events),
            "visitorIds": sorted(visitors),
            "sessionIds": sorted(sessions),
            "eventCounts": dict(event_counts),
            "pageCounts": dict(page_counts),
            "countryCounts": dict(country_counts),
        }
        if granularity == "hour":
            doc["bucket"] = bucket
        else:
            doc["bucket"] = bucket
        docs.append(doc)
    return sorted(docs, key=lambda metric: metric["bucket"])


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
        "sessions": len(session_ids),
        "events": sum(metric_count(metric, "events") for metric in metrics),
    }


def public_bucket_metric(metric: dict, granularity: str) -> dict[str, Any]:
    bucket = format_hour(metric["bucket"]) if granularity == "hour" else metric["bucket"]
    return {
        "bucket": bucket,
        "pageViews": metric_count(metric, "pageViews"),
        "uniqueVisitors": metric_count(metric, "uniqueVisitors"),
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
