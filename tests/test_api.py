import logging
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from analytics_collector.app import create_app
from analytics_collector.config import Settings
from analytics_collector.repository import InMemoryEventRepository


def make_client(geo_lookup=None, **overrides):
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
    app = create_app(settings=settings, repository=repo, geo_lookup=geo_lookup)
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


def test_cf_ipcountry_header_sets_geo_country_without_lookup():
    async def unexpected_lookup(_ip):
        raise AssertionError("IPinfo lookup should not run when CF-IPCountry is present")

    client, repo = make_client(geo_lookup=unexpected_lookup)

    res = client.post(
        "/v1/events/batch",
        json=sample_batch(),
        headers=headers(**{"CF-IPCountry": "US", "X-Forwarded-For": "8.8.8.8"}),
    )

    assert res.status_code == 202
    assert repo.events[0]["geo"] == {"country": "US", "region": None, "city": None}


def test_ipinfo_lookup_sets_geo_country_when_cf_header_is_absent():
    async def lookup(ip):
        assert ip == "8.8.8.8"
        return {"country": "US"}

    client, repo = make_client(geo_lookup=lookup)

    res = client.post(
        "/v1/events/batch",
        json=sample_batch(),
        headers=headers(**{"X-Forwarded-For": "8.8.8.8"}),
    )

    assert res.status_code == 202
    assert repo.events[0]["geo"] == {"country": "US", "region": None, "city": None}


def test_x_forwarded_for_with_port_is_normalized_before_geo_lookup():
    async def lookup(ip):
        assert ip == "8.8.8.8"
        return {"country_code": "US"}

    client, repo = make_client(geo_lookup=lookup)

    res = client.post(
        "/v1/events/batch",
        json=sample_batch(),
        headers=headers(**{"X-Forwarded-For": "8.8.8.8:43210"}),
    )

    assert res.status_code == 202
    assert repo.events[0]["geo"] == {"country": "US", "region": None, "city": None}


def test_x_real_ip_is_used_when_x_forwarded_for_is_absent():
    async def lookup(ip):
        assert ip == "8.8.4.4"
        return {"country_code": "US"}

    client, repo = make_client(geo_lookup=lookup)

    res = client.post(
        "/v1/events/batch",
        json=sample_batch(),
        headers=headers(**{"X-Real-IP": "8.8.4.4"}),
    )

    assert res.status_code == 202
    assert repo.events[0]["geo"] == {"country": "US", "region": None, "city": None}


def test_local_ip_skips_geo_lookup():
    async def unexpected_lookup(_ip):
        raise AssertionError("local IPs should not trigger IPinfo lookup")

    client, repo = make_client(geo_lookup=unexpected_lookup)

    res = client.post(
        "/v1/events/batch",
        json=sample_batch(),
        headers=headers(**{"X-Forwarded-For": "127.0.0.1"}),
    )

    assert res.status_code == 202
    assert repo.events[0]["geo"] == {"country": None, "region": None, "city": None}


def test_geo_debug_logging_explains_skipped_local_ip(caplog):
    client, repo = make_client(geo_lookup_debug=True)

    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        res = client.post(
            "/v1/events/batch",
            json=sample_batch(),
            headers=headers(**{"X-Forwarded-For": "127.0.0.1"}),
        )

    assert res.status_code == 202
    assert repo.events[0]["geo"] == {"country": None, "region": None, "city": None}
    assert "geo lookup skipped: non-public client ip" in caplog.text


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
