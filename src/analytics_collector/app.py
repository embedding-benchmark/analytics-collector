from datetime import UTC, datetime
from hashlib import sha256
from ipaddress import ip_address
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware

from analytics_collector.config import Settings, get_settings
from analytics_collector.geo import Geo, GeoLookup, empty_geo, geo_debug, resolve_geo
from analytics_collector.models import AcceptedResponse, AnalyticsBatch
from analytics_collector.rate_limit import InMemoryRateLimiter
from analytics_collector.repository import EventRepository, MongoEventRepository


def create_app(
    *,
    settings: Settings | None = None,
    repository: EventRepository | None = None,
    geo_lookup: GeoLookup | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    repository = repository or MongoEventRepository(
        settings.mongo_url,
        settings.mongo_database,
        settings.mongo_collection,
    )
    limiter = InMemoryRateLimiter(settings.rate_limit_per_minute)
    app = FastAPI(title="Analytics Collector", version="0.1.0")

    if settings.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.allowed_origins,
            allow_methods=["POST", "GET", "OPTIONS"],
            allow_headers=["content-type", "x-analytics-site-id"],
        )

    app.state.settings = settings
    app.state.repository = repository
    app.state.limiter = limiter
    geo_debug(
        settings,
        "geo lookup debug enabled: has_ipinfo_token=%s timeout_seconds=%s",
        bool(settings.ipinfo_lite_token),
        settings.geo_lookup_timeout_seconds,
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/v1/events/batch",
        response_model=AcceptedResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def collect_events(
        batch: AnalyticsBatch,
        request: Request,
        origin: str | None = Header(default=None),
        referer: str | None = Header(default=None),
        site_id: str | None = Header(default=None, alias="X-Analytics-Site-Id"),
    ) -> AcceptedResponse:
        if not origin_allowed(settings, origin, referer):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="origin not allowed")
        if settings.analytics_site_id and site_id != settings.analytics_site_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid site id")

        ip = client_ip(request)
        rate_key = f"{ip}:{batch.visitorId}:{batch.sessionId}"
        if not limiter.allow(rate_key):
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="rate limited")

        geo = await resolve_geo(ip=ip, headers=request.headers, settings=settings, geo_lookup=geo_lookup)
        enriched = enrich_events(
            batch=batch,
            request=request,
            settings=settings,
            origin=origin,
            referer=referer,
            ip=ip,
            geo=geo,
        )
        await repository.insert_events(enriched)
        return AcceptedResponse(accepted=len(enriched))

    return app


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_repository(request: Request) -> EventRepository:
    return request.app.state.repository


def origin_allowed(settings: Settings, origin: str | None, referer: str | None) -> bool:
    if not settings.allowed_origins:
        return True
    allowed = {o.rstrip("/") for o in settings.allowed_origins}
    if origin and origin.rstrip("/") in allowed:
        return True
    if referer:
        parsed = urlparse(referer)
        referer_origin = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
        return referer_origin in allowed
    return False


def client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        ip = normalize_ip(forwarded_for.split(",", 1)[0])
        if ip:
            return ip
    for header in ("x-real-ip", "cf-connecting-ip"):
        ip = normalize_ip(request.headers.get(header))
        if ip:
            return ip
    if request.client:
        return normalize_ip(request.client.host) or request.client.host
    return "unknown"


def normalize_ip(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().strip('"')
    if not value:
        return None
    if value.startswith("[") and "]" in value:
        value = value[1 : value.index("]")]
    elif value.count(":") == 1:
        host, _port = value.rsplit(":", 1)
        value = host
    try:
        return str(ip_address(value))
    except ValueError:
        return None


def hash_ip(ip: str, salt: str) -> str:
    return sha256(f"{salt}:{ip}".encode("utf-8")).hexdigest()


def enrich_events(
    *,
    batch: AnalyticsBatch,
    request: Request,
    settings: Settings,
    origin: str | None,
    referer: str | None,
    ip: str,
    geo: Geo | None = None,
) -> list[dict]:
    received_at = datetime.now(UTC)
    ip_hash = hash_ip(ip, settings.ip_hash_salt)
    user_agent = request.headers.get("user-agent")
    geo = geo or empty_geo()
    trust = {
        "originOk": True,
        "siteIdOk": True,
        "schemaOk": True,
        "rateLimited": False,
        "source": "browser",
    }
    out: list[dict] = []
    for event in batch.events:
        doc = event.model_dump(mode="json")
        doc.update(
            {
                "visitorId": batch.visitorId,
                "sessionId": batch.sessionId,
                "eventName": event.eventName,
                "receivedAt": received_at,
                "ipHash": ip_hash,
                "userAgent": user_agent,
                "referer": referer,
                "origin": origin,
                "geo": geo.copy(),
                "trust": trust.copy(),
            }
        )
        out.append(doc)
    return out


app = create_app()
