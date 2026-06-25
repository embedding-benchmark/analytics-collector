from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


AnalyticsEventName = Literal[
    "page_view",
    "search_changed",
    "filter_changed",
    "sort_changed",
    "tab_selected",
    "model_pinned",
    "model_unpinned",
    "compare_opened",
    "compare_model_changed",
    "csv_downloaded",
    "share_link_copied",
    "external_link_clicked",
]

ShortString = Annotated[str, Field(min_length=1, max_length=512)]


class PageContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: ShortString
    queryKeys: list[Annotated[str, Field(max_length=128)]] = Field(default_factory=list, max_length=50)


class AnalyticsEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: ShortString
    eventName: AnalyticsEventName
    sentAt: datetime
    page: PageContext
    payload: dict[str, Any] = Field(default_factory=dict)


class AnalyticsBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    visitorId: ShortString
    sessionId: ShortString
    events: list[AnalyticsEvent] = Field(min_length=1, max_length=50)


class AcceptedResponse(BaseModel):
    accepted: int


class AnalyticsDateRange(BaseModel):
    startDate: date
    endDate: date


class AggregateResponse(BaseModel):
    status: str
    rawEvents: int
