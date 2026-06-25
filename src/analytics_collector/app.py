from datetime import UTC, date, datetime
from hashlib import sha256
from ipaddress import ip_address
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException, Query, Request, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from analytics_collector.analytics import aggregate_range, compare_distribution, distribution, domain_distribution, funnels, retention, summary
from analytics_collector.config import Settings, get_settings
from analytics_collector.geo import Geo, GeoLookup, empty_geo, geo_debug, resolve_geo
from analytics_collector.models import AcceptedResponse, AggregateResponse, AnalyticsBatch, AnalyticsDateRange
from analytics_collector.rate_limit import InMemoryRateLimiter
from analytics_collector.repository import EventRepository, MongoEventRepository


admin_bearer = HTTPBearer(auto_error=False)


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
        hourly_collection=settings.mongo_hourly_collection,
        daily_collection=settings.mongo_daily_collection,
        funnel_collection=settings.mongo_funnel_collection,
        retention_collection=settings.mongo_retention_collection,
    )
    limiter = InMemoryRateLimiter(settings.rate_limit_per_minute)
    app = FastAPI(title="Analytics Collector", version="0.1.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins or ["*"],
        allow_methods=["POST", "GET", "OPTIONS"],
        allow_headers=["authorization", "content-type", "x-analytics-site-id"],
        allow_credentials=False,
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

    @app.post(
        "/v1/analytics/aggregate",
        response_model=AggregateResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def aggregate_metrics(
        date_range: AnalyticsDateRange,
        credentials: HTTPAuthorizationCredentials | None = Security(admin_bearer),
    ) -> AggregateResponse:
        require_admin(settings, credentials)
        try:
            result = await aggregate_range(repository, date_range.startDate, date_range.endDate)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        return AggregateResponse(**result)

    @app.get("/v1/analytics/summary")
    async def analytics_summary(
        start_date: date = Query(alias="startDate"),
        end_date: date = Query(alias="endDate"),
        credentials: HTTPAuthorizationCredentials | None = Security(admin_bearer),
    ) -> dict:
        require_admin(settings, credentials)
        return await run_query(summary(repository, start_date, end_date))

    @app.get("/v1/analytics/events")
    async def analytics_events(
        start_date: date = Query(alias="startDate"),
        end_date: date = Query(alias="endDate"),
        credentials: HTTPAuthorizationCredentials | None = Security(admin_bearer),
    ) -> dict:
        require_admin(settings, credentials)
        return await run_query(distribution(repository, start_date, end_date, "events"))

    @app.get("/v1/analytics/pages")
    async def analytics_pages(
        start_date: date = Query(alias="startDate"),
        end_date: date = Query(alias="endDate"),
        credentials: HTTPAuthorizationCredentials | None = Security(admin_bearer),
    ) -> dict:
        require_admin(settings, credentials)
        return await run_query(distribution(repository, start_date, end_date, "pages"))

    @app.get("/v1/analytics/countries")
    async def analytics_countries(
        start_date: date = Query(alias="startDate"),
        end_date: date = Query(alias="endDate"),
        credentials: HTTPAuthorizationCredentials | None = Security(admin_bearer),
    ) -> dict:
        require_admin(settings, credentials)
        return await run_query(distribution(repository, start_date, end_date, "countries"))

    @app.get("/v1/analytics/benchmarks")
    async def analytics_benchmarks(
        start_date: date = Query(alias="startDate"),
        end_date: date = Query(alias="endDate"),
        credentials: HTTPAuthorizationCredentials | None = Security(admin_bearer),
    ) -> dict:
        require_admin(settings, credentials)
        return await run_query(domain_distribution(repository, start_date, end_date, "benchmarks"))

    @app.get("/v1/analytics/models")
    async def analytics_models(
        start_date: date = Query(alias="startDate"),
        end_date: date = Query(alias="endDate"),
        credentials: HTTPAuthorizationCredentials | None = Security(admin_bearer),
    ) -> dict:
        require_admin(settings, credentials)
        return await run_query(domain_distribution(repository, start_date, end_date, "models"))

    @app.get("/v1/analytics/searches")
    async def analytics_searches(
        start_date: date = Query(alias="startDate"),
        end_date: date = Query(alias="endDate"),
        credentials: HTTPAuthorizationCredentials | None = Security(admin_bearer),
    ) -> dict:
        require_admin(settings, credentials)
        return await run_query(domain_distribution(repository, start_date, end_date, "searches"))

    @app.get("/v1/analytics/filters")
    async def analytics_filters(
        start_date: date = Query(alias="startDate"),
        end_date: date = Query(alias="endDate"),
        credentials: HTTPAuthorizationCredentials | None = Security(admin_bearer),
    ) -> dict:
        require_admin(settings, credentials)
        return await run_query(domain_distribution(repository, start_date, end_date, "filters"))

    @app.get("/v1/analytics/compares")
    async def analytics_compares(
        start_date: date = Query(alias="startDate"),
        end_date: date = Query(alias="endDate"),
        credentials: HTTPAuthorizationCredentials | None = Security(admin_bearer),
    ) -> dict:
        require_admin(settings, credentials)
        return await run_query(compare_distribution(repository, start_date, end_date))

    @app.get("/v1/analytics/tasks")
    async def analytics_tasks(
        start_date: date = Query(alias="startDate"),
        end_date: date = Query(alias="endDate"),
        credentials: HTTPAuthorizationCredentials | None = Security(admin_bearer),
    ) -> dict:
        require_admin(settings, credentials)
        return await run_query(domain_distribution(repository, start_date, end_date, "tasks"))

    @app.get("/v1/analytics/funnels")
    async def analytics_funnels(
        start_date: date = Query(alias="startDate"),
        end_date: date = Query(alias="endDate"),
        credentials: HTTPAuthorizationCredentials | None = Security(admin_bearer),
    ) -> dict:
        require_admin(settings, credentials)
        return await run_query(funnels(repository, start_date, end_date))

    @app.get("/v1/analytics/retention")
    async def analytics_retention(
        start_date: date = Query(alias="startDate"),
        end_date: date = Query(alias="endDate"),
        credentials: HTTPAuthorizationCredentials | None = Security(admin_bearer),
    ) -> dict:
        require_admin(settings, credentials)
        return await run_query(retention(repository, start_date, end_date))

    return app


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_repository(request: Request) -> EventRepository:
    return request.app.state.repository


def require_admin(settings: Settings, credentials: HTTPAuthorizationCredentials | None) -> None:
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing authorization")
    if not settings.analytics_admin_token:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="analytics admin token not configured")
    if credentials.scheme.lower() != "bearer" or credentials.credentials != settings.analytics_admin_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid authorization")


async def run_query(coro):
    try:
        return await coro
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


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
