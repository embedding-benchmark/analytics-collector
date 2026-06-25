from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from analytics_collector.app import create_app
from analytics_collector.config import Settings
from analytics_collector.repository import InMemoryEventRepository


ADMIN_TOKEN = "test-admin-token"


def make_client():
    repo = InMemoryEventRepository()
    settings = Settings(
        allowed_origins=["https://leaderboard.example"],
        analytics_admin_token=ADMIN_TOKEN,
        mongo_url="mongodb://localhost:27017",
        mongo_database="analytics",
    )
    app = create_app(settings=settings, repository=repo)
    return TestClient(app), repo


def auth_headers():
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def event_headers():
    return {"Origin": "https://leaderboard.example", "User-Agent": "pytest"}


def event(
    *,
    visitor_id="visitor-1",
    session_id="session-1",
    event_name="page_view",
    received_at: datetime,
    page_path="/models",
    country="US",
    payload=None,
):
    payload = {"path": page_path} if payload is None else payload
    return {
        "visitorId": visitor_id,
        "sessionId": session_id,
        "id": f"{visitor_id}-{session_id}-{event_name}-{received_at.timestamp()}",
        "eventName": event_name,
        "sentAt": received_at.isoformat(),
        "receivedAt": received_at,
        "page": {"path": page_path, "queryKeys": []},
        "payload": payload,
        "geo": {"country": country, "region": None, "city": None},
        "trust": {"originOk": True, "siteIdOk": True, "schemaOk": True, "rateLimited": False, "source": "browser"},
    }


def aggregate(client, start_date="2026-06-01", end_date="2026-06-02"):
    return client.post(
        "/v1/analytics/aggregate",
        json={"startDate": start_date, "endDate": end_date},
        headers=auth_headers(),
    )


def test_analytics_endpoints_require_admin_token():
    client, _repo = make_client()

    missing = client.get("/v1/analytics/summary?startDate=2026-06-01&endDate=2026-06-02")
    wrong = client.get(
        "/v1/analytics/summary?startDate=2026-06-01&endDate=2026-06-02",
        headers={"Authorization": "Bearer wrong"},
    )

    assert missing.status_code == 401
    assert wrong.status_code == 403


def test_analytics_openapi_declares_bearer_security():
    client, _repo = make_client()

    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/v1/analytics/aggregate"]["post"]

    assert schema["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
    assert operation["security"] == [{"HTTPBearer": []}]


def test_analytics_endpoints_allow_authorization_cors_preflight():
    client, _repo = make_client()

    response = client.options(
        "/v1/analytics/summary?startDate=2026-06-01&endDate=2026-06-01",
        headers={
            "Origin": "https://leaderboard.example",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )

    assert response.status_code == 200
    assert "authorization" in response.headers["access-control-allow-headers"].lower()


def test_aggregate_builds_summary_series_and_is_idempotent():
    client, repo = make_client()
    repo.events.extend(
        [
            event(visitor_id="visitor-1", session_id="session-1", received_at=datetime(2026, 6, 1, 10, 15, tzinfo=UTC)),
            event(visitor_id="visitor-1", session_id="session-1", event_name="search_changed", received_at=datetime(2026, 6, 1, 10, 16, tzinfo=UTC)),
            event(visitor_id="visitor-2", session_id="session-2", received_at=datetime(2026, 6, 1, 11, 0, tzinfo=UTC)),
            event(visitor_id="visitor-1", session_id="session-1", event_name="filter_changed", received_at=datetime(2026, 6, 1, 11, 15, tzinfo=UTC)),
            event(visitor_id="visitor-3", session_id="session-3", received_at=datetime(2026, 6, 3, 9, 0, tzinfo=UTC)),
        ]
    )

    first = aggregate(client)
    second = aggregate(client)
    summary = client.get(
        "/v1/analytics/summary?startDate=2026-06-01&endDate=2026-06-02",
        headers=auth_headers(),
    )

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["rawEvents"] == 4
    assert summary.status_code == 200
    body = summary.json()
    assert body["granularity"] == "hour"
    assert body["totals"] == {"pageViews": 2, "uniqueVisitors": 2, "newVisitors": 2, "returningVisitors": 1, "sessions": 2, "events": 4}
    assert body["series"] == [
        {
            "bucket": "2026-06-01T10:00:00Z",
            "pageViews": 1,
            "uniqueVisitors": 1,
            "newVisitors": 1,
            "returningVisitors": 0,
            "sessions": 1,
            "events": 2,
        },
        {
            "bucket": "2026-06-01T11:00:00Z",
            "pageViews": 1,
            "uniqueVisitors": 2,
            "newVisitors": 1,
            "returningVisitors": 1,
            "sessions": 2,
            "events": 2,
        },
    ]


def test_long_range_summary_uses_daily_granularity():
    client, repo = make_client()
    repo.events.extend(
        [
            event(received_at=datetime(2026, 6, 1, 10, 0, tzinfo=UTC)),
            event(visitor_id="visitor-2", session_id="session-2", received_at=datetime(2026, 6, 10, 10, 0, tzinfo=UTC)),
        ]
    )

    aggregate(client, "2026-06-01", "2026-06-15")
    summary = client.get(
        "/v1/analytics/summary?startDate=2026-06-01&endDate=2026-06-15",
        headers=auth_headers(),
    )

    assert summary.status_code == 200
    assert summary.json()["granularity"] == "day"
    assert summary.json()["series"] == [
        {
            "bucket": "2026-06-01",
            "pageViews": 1,
            "uniqueVisitors": 1,
            "newVisitors": 1,
            "returningVisitors": 0,
            "sessions": 1,
            "events": 1,
        },
        {
            "bucket": "2026-06-10",
            "pageViews": 1,
            "uniqueVisitors": 1,
            "newVisitors": 1,
            "returningVisitors": 0,
            "sessions": 1,
            "events": 1,
        },
    ]


def test_distribution_endpoints_return_events_pages_and_countries():
    client, repo = make_client()
    repo.events.extend(
        [
            event(event_name="page_view", received_at=datetime(2026, 6, 1, 10, 0, tzinfo=UTC), page_path="/models", country="US"),
            event(event_name="page_view", received_at=datetime(2026, 6, 1, 11, 0, tzinfo=UTC), page_path="/models", country="US"),
            event(event_name="csv_downloaded", received_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC), page_path="/exports", country="JP"),
        ]
    )
    aggregate(client)

    events = client.get("/v1/analytics/events?startDate=2026-06-01&endDate=2026-06-02", headers=auth_headers())
    pages = client.get("/v1/analytics/pages?startDate=2026-06-01&endDate=2026-06-02", headers=auth_headers())
    countries = client.get("/v1/analytics/countries?startDate=2026-06-01&endDate=2026-06-02", headers=auth_headers())

    assert events.json()["items"] == [{"eventName": "page_view", "count": 2}, {"eventName": "csv_downloaded", "count": 1}]
    assert pages.json()["items"] == [{"path": "/models", "count": 2}, {"path": "/exports", "count": 1}]
    assert countries.json()["items"] == [{"country": "US", "count": 2}, {"country": "JP", "count": 1}]


def test_compare_event_is_ingested_and_payload_model_is_queryable():
    client, repo = make_client()
    model = "sentence-transformers/all-MiniLM-L6-v2"
    batch = {
        "visitorId": "visitor-compare",
        "sessionId": "session-compare",
        "events": [
            {
                "id": "evt-compare-model-added",
                "eventName": "compare_model_changed",
                "sentAt": datetime.now(UTC).isoformat(),
                "page": {"path": "/compare", "queryKeys": []},
                "payload": {
                    "action": "added",
                    "benchmark": "MTEB(eng, v2)",
                    "model": model,
                    "modelCount": 2,
                },
            }
        ],
    }

    ingest = client.post("/v1/events/batch", json=batch, headers=event_headers())

    assert ingest.status_code == 202
    assert ingest.json() == {"accepted": 1}
    assert len(repo.events) == 1
    saved = repo.events[0]
    assert saved["eventName"] == "compare_model_changed"
    assert saved["payload"]["model"] == model

    metric_date = saved["receivedAt"].date().isoformat()
    aggregate(client, metric_date, metric_date)
    compares = client.get(
        f"/v1/analytics/compares?startDate={metric_date}&endDate={metric_date}",
        headers=auth_headers(),
    )

    assert compares.status_code == 200
    assert compares.json()["models"] == [{"model": model, "sessions": 1, "events": 1, "visitors": 1}]


def test_domain_distribution_endpoints_rank_by_sessions_with_event_and_visitor_context():
    client, repo = make_client()
    base = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
    repo.events.extend(
        [
            event(
                visitor_id="visitor-1",
                session_id="session-1",
                event_name="page_view",
                received_at=base,
                payload={"benchmarkId": "MTEB(eng, v2)", "modelId": "bge-large-en"},
            ),
            event(
                visitor_id="visitor-1",
                session_id="session-1",
                event_name="page_view",
                received_at=base + timedelta(minutes=1),
                payload={"benchmarkName": "MTEB English v2", "modelName": "BGE Large EN"},
            ),
            event(
                visitor_id="visitor-2",
                session_id="session-2",
                event_name="search_changed",
                received_at=base + timedelta(minutes=2),
                payload={"query": "retrieval", "surface": "models"},
            ),
            event(
                visitor_id="visitor-2",
                session_id="session-2",
                event_name="search_changed",
                received_at=base + timedelta(minutes=3),
                payload={"query": "retrieval", "surface": "models"},
            ),
            event(
                visitor_id="visitor-3",
                session_id="session-3",
                event_name="filter_changed",
                received_at=base + timedelta(minutes=4),
                payload={"filterKey": "task", "filterValue": "Retrieval", "task": "Retrieval", "surface": "benchmarks"},
            ),
            event(
                visitor_id="visitor-4",
                session_id="session-4",
                event_name="compare_opened",
                received_at=base + timedelta(minutes=5),
                payload={"models": ["bge-large-en", "e5-large-v2"], "benchmarkId": "MTEB(eng, v2)"},
            ),
            event(
                visitor_id="visitor-5",
                session_id="session-5",
                event_name="compare_model_changed",
                received_at=base + timedelta(minutes=6),
                payload={"modelIds": ["bge-large-en", "gte-large"], "benchmarkId": "MTEB(eng, v2)"},
            ),
            event(
                visitor_id="visitor-6",
                session_id="session-6",
                event_name="filter_changed",
                received_at=base + timedelta(days=2),
                payload={"filterKey": "language", "filterValue": "English"},
            ),
            event(
                visitor_id="visitor-7",
                session_id="session-7",
                event_name="search_changed",
                received_at=base + timedelta(minutes=7),
                payload={"query": ""},
            ),
            event(
                visitor_id="visitor-8",
                session_id="session-8",
                event_name="page_view",
                received_at=base + timedelta(minutes=8),
                page_path="/benchmarks/mteb-english-v2",
                payload={"path": "/benchmarks/mteb-english-v2", "title": "MTEB English v2"},
            ),
            event(
                visitor_id="visitor-9",
                session_id="session-9",
                event_name="page_view",
                received_at=base + timedelta(minutes=9),
                page_path="/models/gte-large",
                payload={"path": "/models/gte-large", "title": "GTE Large"},
            ),
            event(
                visitor_id="visitor-10",
                session_id="session-10",
                event_name="page_view",
                received_at=base + timedelta(minutes=10),
                page_path="/tasks/reranking",
                payload={"path": "/tasks/reranking", "title": "Reranking"},
            ),
        ]
    )
    aggregate(client)

    benchmarks = client.get("/v1/analytics/benchmarks?startDate=2026-06-01&endDate=2026-06-02", headers=auth_headers())
    models = client.get("/v1/analytics/models?startDate=2026-06-01&endDate=2026-06-02", headers=auth_headers())
    searches = client.get("/v1/analytics/searches?startDate=2026-06-01&endDate=2026-06-02", headers=auth_headers())
    filters = client.get("/v1/analytics/filters?startDate=2026-06-01&endDate=2026-06-02", headers=auth_headers())
    compares = client.get("/v1/analytics/compares?startDate=2026-06-01&endDate=2026-06-02", headers=auth_headers())
    tasks = client.get("/v1/analytics/tasks?startDate=2026-06-01&endDate=2026-06-02", headers=auth_headers())

    assert benchmarks.status_code == 200
    assert benchmarks.json()["items"] == [
        {"benchmark": "MTEB English v2", "sessions": 2, "events": 2, "visitors": 2},
        {"benchmark": "MTEB(eng, v2)", "sessions": 1, "events": 1, "visitors": 1},
    ]
    assert models.json()["items"] == [
        {"model": "BGE Large EN", "sessions": 1, "events": 1, "visitors": 1},
        {"model": "GTE Large", "sessions": 1, "events": 1, "visitors": 1},
        {"model": "bge-large-en", "sessions": 1, "events": 1, "visitors": 1},
    ]
    assert searches.json()["items"] == [{"query": "retrieval", "sessions": 1, "events": 2, "visitors": 1}]
    assert filters.json()["items"] == [{"filterKey": "task", "filterValue": "Retrieval", "sessions": 1, "events": 1, "visitors": 1}]
    assert compares.json()["items"] == [
        {"comparison": "bge-large-en vs e5-large-v2", "sessions": 1, "events": 1, "visitors": 1},
        {"comparison": "bge-large-en vs gte-large", "sessions": 1, "events": 1, "visitors": 1},
    ]
    assert compares.json()["benchmarks"] == [{"benchmark": "MTEB(eng, v2)", "sessions": 2, "events": 2, "visitors": 2}]
    assert compares.json()["models"] == [
        {"model": "bge-large-en", "sessions": 2, "events": 2, "visitors": 2},
        {"model": "e5-large-v2", "sessions": 1, "events": 1, "visitors": 1},
        {"model": "gte-large", "sessions": 1, "events": 1, "visitors": 1},
    ]
    assert tasks.json()["items"] == [
        {"task": "Reranking", "sessions": 1, "events": 1, "visitors": 1},
        {"task": "Retrieval", "sessions": 1, "events": 1, "visitors": 1},
    ]
    day_metric = repo.daily_metrics[0]
    assert day_metric["benchmarkMetrics"]["MTEB English v2"]["pathCounts"]["/benchmarks/mteb-english-v2"] == 1
    assert day_metric["benchmarkMetrics"]["MTEB English v2"]["titleCounts"] == {"MTEB English v2": 1}


def test_compare_events_count_current_benchmark_and_model_fields():
    client, repo = make_client()
    base = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
    repo.events.extend(
        [
            event(
                visitor_id="visitor-1",
                session_id="session-1",
                event_name="compare_opened",
                received_at=base,
                payload={"source": "pinned_chip", "modelCount": 3, "benchmark": "MTEB(eng, v2)"},
            ),
            event(
                visitor_id="visitor-2",
                session_id="session-2",
                event_name="compare_model_changed",
                received_at=base + timedelta(minutes=1),
                payload={
                    "action": "added",
                    "benchmark": "MTEB(eng, v2)",
                    "model": "sentence-transformers/all-MiniLM-L6-v2",
                    "modelCount": 2,
                },
            ),
            event(
                visitor_id="visitor-3",
                session_id="session-3",
                event_name="compare_model_changed",
                received_at=base + timedelta(minutes=2),
                payload={"action": "cleared", "benchmark": "MTEB(eng, v2)", "model": None, "modelCount": 0},
            ),
        ]
    )
    aggregate(client)

    benchmarks = client.get("/v1/analytics/benchmarks?startDate=2026-06-01&endDate=2026-06-02", headers=auth_headers())
    models = client.get("/v1/analytics/models?startDate=2026-06-01&endDate=2026-06-02", headers=auth_headers())
    compares = client.get("/v1/analytics/compares?startDate=2026-06-01&endDate=2026-06-02", headers=auth_headers())

    assert benchmarks.json()["items"] == []
    assert models.json()["items"] == []
    assert compares.json()["benchmarks"] == [{"benchmark": "MTEB(eng, v2)", "sessions": 3, "events": 3, "visitors": 3}]
    assert compares.json()["models"] == [
        {"model": "sentence-transformers/all-MiniLM-L6-v2", "sessions": 1, "events": 1, "visitors": 1}
    ]


def test_funnel_counts_ordered_steps_within_same_session():
    client, repo = make_client()
    base = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
    repo.events.extend(
        [
            event(session_id="session-1", event_name="page_view", received_at=base),
            event(session_id="session-1", event_name="search_changed", received_at=base + timedelta(minutes=1)),
            event(session_id="session-1", event_name="compare_opened", received_at=base + timedelta(minutes=2)),
            event(session_id="session-1", event_name="csv_downloaded", received_at=base + timedelta(minutes=3)),
            event(visitor_id="visitor-2", session_id="session-2", event_name="page_view", received_at=base),
            event(visitor_id="visitor-2", session_id="session-2", event_name="compare_opened", received_at=base + timedelta(minutes=1)),
            event(visitor_id="visitor-3", session_id="session-3", event_name="search_changed", received_at=base),
            event(visitor_id="visitor-3", session_id="session-3", event_name="page_view", received_at=base + timedelta(minutes=1)),
        ]
    )
    aggregate(client)

    response = client.get("/v1/analytics/funnels?startDate=2026-06-01&endDate=2026-06-02", headers=auth_headers())

    assert response.status_code == 200
    assert response.json()["funnels"] == [
        {
            "name": "engagement",
            "steps": [
                {"step": "page_view", "sessions": 3, "conversionRate": 1.0},
                {"step": "search_or_filter", "sessions": 1, "conversionRate": 1 / 3},
                {"step": "compare_opened", "sessions": 1, "conversionRate": 1 / 3},
                {"step": "download_or_external_click", "sessions": 1, "conversionRate": 1 / 3},
            ],
        }
    ]


def test_retention_counts_d1_d7_d30_returning_visitors_by_first_seen_cohort():
    client, repo = make_client()
    cohort = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
    repo.events.extend(
        [
            event(visitor_id="visitor-1", session_id="s1", received_at=cohort),
            event(visitor_id="visitor-1", session_id="s2", received_at=cohort + timedelta(days=1)),
            event(visitor_id="visitor-1", session_id="s3", received_at=cohort + timedelta(days=7)),
            event(visitor_id="visitor-2", session_id="s4", received_at=cohort),
            event(visitor_id="visitor-2", session_id="s5", received_at=cohort + timedelta(days=30)),
            event(visitor_id="visitor-3", session_id="s6", received_at=cohort + timedelta(days=1)),
        ]
    )
    aggregate(client, "2026-06-01", "2026-07-01")

    response = client.get("/v1/analytics/retention?startDate=2026-06-01&endDate=2026-06-01", headers=auth_headers())

    assert response.status_code == 200
    assert response.json()["cohorts"] == [
        {
            "cohortDate": "2026-06-01",
            "size": 2,
            "d1": 1,
            "d1Rate": 0.5,
            "d7": 1,
            "d7Rate": 0.5,
            "d30": 1,
            "d30Rate": 0.5,
        }
    ]
