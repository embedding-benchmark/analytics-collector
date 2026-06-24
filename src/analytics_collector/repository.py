from typing import Protocol

from motor.motor_asyncio import AsyncIOMotorClient


class EventRepository(Protocol):
    async def insert_events(self, events: list[dict]) -> None: ...


class InMemoryEventRepository:
    def __init__(self):
        self.events: list[dict] = []

    async def insert_events(self, events: list[dict]) -> None:
        self.events.extend(events)


class MongoEventRepository:
    def __init__(self, mongo_url: str, database: str, collection: str):
        self._client = AsyncIOMotorClient(mongo_url)
        self._collection = self._client[database][collection]

    async def insert_events(self, events: list[dict]) -> None:
        if events:
            await self._collection.insert_many(events)

