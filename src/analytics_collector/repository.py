from datetime import date, datetime
from typing import Protocol

from motor.motor_asyncio import AsyncIOMotorClient


class EventRepository(Protocol):
    async def insert_events(self, events: list[dict]) -> None: ...
    async def fetch_events(self, start: datetime | None, end: datetime | None) -> list[dict]: ...
    async def replace_hourly_metrics(self, start_date: date, end_date: date, metrics: list[dict]) -> None: ...
    async def replace_daily_metrics(self, start_date: date, end_date: date, metrics: list[dict]) -> None: ...
    async def replace_funnel_metrics(self, start_date: date, end_date: date, metrics: list[dict]) -> None: ...
    async def replace_retention_metrics(self, start_date: date, end_date: date, metrics: list[dict]) -> None: ...
    async def list_hourly_metrics(self, start_date: date, end_date: date) -> list[dict]: ...
    async def list_daily_metrics(self, start_date: date, end_date: date) -> list[dict]: ...
    async def list_funnel_metrics(self, start_date: date, end_date: date) -> list[dict]: ...
    async def list_retention_metrics(self, start_date: date, end_date: date) -> list[dict]: ...


class InMemoryEventRepository:
    def __init__(self):
        self.events: list[dict] = []
        self.hourly_metrics: list[dict] = []
        self.daily_metrics: list[dict] = []
        self.funnel_metrics: list[dict] = []
        self.retention_metrics: list[dict] = []

    async def insert_events(self, events: list[dict]) -> None:
        self.events.extend(events)

    async def fetch_events(self, start: datetime | None, end: datetime | None) -> list[dict]:
        out = []
        for event in self.events:
            received_at = event["receivedAt"]
            if start and received_at < start:
                continue
            if end and received_at >= end:
                continue
            out.append(event.copy())
        return out

    async def replace_hourly_metrics(self, start_date: date, end_date: date, metrics: list[dict]) -> None:
        self.hourly_metrics = replace_by_date(self.hourly_metrics, start_date, end_date, metrics, "date")

    async def replace_daily_metrics(self, start_date: date, end_date: date, metrics: list[dict]) -> None:
        self.daily_metrics = replace_by_date(self.daily_metrics, start_date, end_date, metrics, "date")

    async def replace_funnel_metrics(self, start_date: date, end_date: date, metrics: list[dict]) -> None:
        self.funnel_metrics = replace_by_date(self.funnel_metrics, start_date, end_date, metrics, "date")

    async def replace_retention_metrics(self, start_date: date, end_date: date, metrics: list[dict]) -> None:
        self.retention_metrics = replace_by_date(self.retention_metrics, start_date, end_date, metrics, "cohortDate")

    async def list_hourly_metrics(self, start_date: date, end_date: date) -> list[dict]:
        return filter_by_date(self.hourly_metrics, start_date, end_date, "date")

    async def list_daily_metrics(self, start_date: date, end_date: date) -> list[dict]:
        return filter_by_date(self.daily_metrics, start_date, end_date, "date")

    async def list_funnel_metrics(self, start_date: date, end_date: date) -> list[dict]:
        return filter_by_date(self.funnel_metrics, start_date, end_date, "date")

    async def list_retention_metrics(self, start_date: date, end_date: date) -> list[dict]:
        return filter_by_date(self.retention_metrics, start_date, end_date, "cohortDate")


class MongoEventRepository:
    def __init__(
        self,
        mongo_url: str,
        database: str,
        collection: str,
        *,
        hourly_collection: str = "analytics_hourly_metrics",
        daily_collection: str = "analytics_daily_metrics",
        funnel_collection: str = "analytics_funnel_metrics",
        retention_collection: str = "analytics_retention_metrics",
    ):
        self._client = AsyncIOMotorClient(mongo_url)
        db = self._client[database]
        self._collection = db[collection]
        self._hourly_collection = db[hourly_collection]
        self._daily_collection = db[daily_collection]
        self._funnel_collection = db[funnel_collection]
        self._retention_collection = db[retention_collection]

    async def insert_events(self, events: list[dict]) -> None:
        if events:
            await self._collection.insert_many(events)

    async def fetch_events(self, start: datetime | None, end: datetime | None) -> list[dict]:
        query = {}
        range_query = {}
        if start:
            range_query["$gte"] = start
        if end:
            range_query["$lt"] = end
        if range_query:
            query["receivedAt"] = range_query
        return [event async for event in self._collection.find(query).sort("receivedAt", 1)]

    async def replace_hourly_metrics(self, start_date: date, end_date: date, metrics: list[dict]) -> None:
        await replace_mongo_by_date(self._hourly_collection, start_date, end_date, metrics, "date")

    async def replace_daily_metrics(self, start_date: date, end_date: date, metrics: list[dict]) -> None:
        await replace_mongo_by_date(self._daily_collection, start_date, end_date, metrics, "date")

    async def replace_funnel_metrics(self, start_date: date, end_date: date, metrics: list[dict]) -> None:
        await replace_mongo_by_date(self._funnel_collection, start_date, end_date, metrics, "date")

    async def replace_retention_metrics(self, start_date: date, end_date: date, metrics: list[dict]) -> None:
        await replace_mongo_by_date(self._retention_collection, start_date, end_date, metrics, "cohortDate")

    async def list_hourly_metrics(self, start_date: date, end_date: date) -> list[dict]:
        return await list_mongo_by_date(self._hourly_collection, start_date, end_date, "date")

    async def list_daily_metrics(self, start_date: date, end_date: date) -> list[dict]:
        return await list_mongo_by_date(self._daily_collection, start_date, end_date, "date")

    async def list_funnel_metrics(self, start_date: date, end_date: date) -> list[dict]:
        return await list_mongo_by_date(self._funnel_collection, start_date, end_date, "date")

    async def list_retention_metrics(self, start_date: date, end_date: date) -> list[dict]:
        return await list_mongo_by_date(self._retention_collection, start_date, end_date, "cohortDate")


def replace_by_date(existing: list[dict], start_date: date, end_date: date, metrics: list[dict], field: str) -> list[dict]:
    kept = [metric for metric in existing if not in_date_range(metric[field], start_date, end_date)]
    return sorted([*kept, *metrics], key=lambda metric: metric[field])


def filter_by_date(existing: list[dict], start_date: date, end_date: date, field: str) -> list[dict]:
    return sorted(
        [metric.copy() for metric in existing if in_date_range(metric[field], start_date, end_date)],
        key=lambda metric: metric[field],
    )


def in_date_range(value: str, start_date: date, end_date: date) -> bool:
    metric_date = date.fromisoformat(value)
    return start_date <= metric_date <= end_date


async def replace_mongo_by_date(collection, start_date: date, end_date: date, metrics: list[dict], field: str) -> None:
    await collection.delete_many({field: {"$gte": start_date.isoformat(), "$lte": end_date.isoformat()}})
    if metrics:
        await collection.insert_many(metrics)


async def list_mongo_by_date(collection, start_date: date, end_date: date, field: str) -> list[dict]:
    query = {field: {"$gte": start_date.isoformat(), "$lte": end_date.isoformat()}}
    return [metric async for metric in collection.find(query, {"_id": 0}).sort(field, 1)]
