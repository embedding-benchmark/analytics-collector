from datetime import UTC, datetime

from fastapi.testclient import TestClient

from analytics_collector.app import create_app
from analytics_collector.config import Settings
from analytics_collector.repository import InMemoryEventRepository


def make_client(**overrides):
    repo = InMemoryEventRepository()
    config = {
        "allowed_origins": ["https://leaderboard.example"],
        "analytics_site_id": None,
        "rate_limit_per_minute": 3,
        "mongo_url": "mongodb://localhost:27017",
        "mongo_database": "analytics",
    }
    config.update(overrides)
    settings = Settings(**config)
    app = create_app(settings=settings, repository=repo)
    return TestClient(app), repo


def sample_event(event_name="page_view"):
    return {
        "id": "evt-1",
        "eventName": event_name,
        "sentAt": datetime.now(UTC).isoformat(),
        "page": {"path": "/models", "queryKeys": ["q"]},
        "payload": {"path": "/models", "queryKeys": ["q"]},
    }


def sample_batch(**overrides):
    batch = {
        "visitorId": "visitor-1",
        "sessionId": "session-1",
        "events": [sample_event()],
    }
    batch.update(overrides)
    return batch


def headers(**overrides):
    base = {"Origin": "https://leaderboard.example", "User-Agent": "pytest"}
    base.update(overrides)
    return base


def test_health_returns_ok():
    client, _ = make_client()

    res = client.get("/health")

    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_valid_batch_is_accepted_and_enriched():
    client, repo = make_client()

    res = client.post("/v1/events/batch", json=sample_batch(), headers=headers())

    assert res.status_code == 202
    assert res.json() == {"accepted": 1}
    assert len(repo.events) == 1
    saved = repo.events[0]
    assert saved["visitorId"] == "visitor-1"
    assert saved["sessionId"] == "session-1"
    assert saved["eventName"] == "page_view"
    assert saved["receivedAt"]
    assert saved["ipHash"]
    assert saved["userAgent"] == "pytest"
    assert saved["origin"] == "https://leaderboard.example"
    assert saved["trust"] == {
        "originOk": True,
        "siteIdOk": True,
        "schemaOk": True,
        "rateLimited": False,
        "source": "browser",
    }


def test_unknown_event_name_is_rejected():
    client, _ = make_client()
    batch = sample_batch(events=[sample_event("not_real")])

    res = client.post("/v1/events/batch", json=batch, headers=headers())

    assert res.status_code == 422


def test_bad_origin_is_rejected():
    client, repo = make_client()

    res = client.post(
        "/v1/events/batch",
        json=sample_batch(),
        headers=headers(Origin="https://evil.example"),
    )

    assert res.status_code == 403
    assert repo.events == []


def test_site_id_required_when_configured():
    client, _ = make_client(analytics_site_id="site-1")

    missing = client.post("/v1/events/batch", json=sample_batch(), headers=headers())
    accepted = client.post(
        "/v1/events/batch",
        json=sample_batch(),
        headers=headers(**{"X-Analytics-Site-Id": "site-1"}),
    )

    assert missing.status_code == 403
    assert accepted.status_code == 202


def test_rate_limit_rejects_excess_batches():
    client, _ = make_client(rate_limit_per_minute=1)

    first = client.post("/v1/events/batch", json=sample_batch(), headers=headers())
    second = client.post("/v1/events/batch", json=sample_batch(), headers=headers())

    assert first.status_code == 202
    assert second.status_code == 429
