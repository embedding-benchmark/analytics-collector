from collections.abc import Awaitable, Callable, Mapping
from ipaddress import ip_address
import logging
from typing import Any

import httpx

from analytics_collector.config import Settings


Geo = dict[str, str | None]
GeoLookup = Callable[[str], Awaitable[Mapping[str, Any] | None]]
logger = logging.getLogger("uvicorn.error")


def empty_geo() -> Geo:
    return {"country": None, "region": None, "city": None}


async def resolve_geo(
    *,
    ip: str,
    headers: Mapping[str, str],
    settings: Settings,
    geo_lookup: GeoLookup | None = None,
) -> Geo:
    geo_debug(settings, "geo lookup start: ip=%s cf_ipcountry=%s has_ipinfo_token=%s", ip, headers.get("cf-ipcountry"), bool(settings.ipinfo_lite_token))

    country = country_from_cloudflare(headers)
    if country:
        geo_debug(settings, "geo lookup resolved from CF-IPCountry: country=%s", country)
        return {"country": country, "region": None, "city": None}

    if not is_global_ip(ip):
        geo_debug(settings, "geo lookup skipped: non-public client ip ip=%s", ip)
        return empty_geo()

    lookup = geo_lookup or ipinfo_lite_lookup(settings)
    if not lookup:
        geo_debug(settings, "geo lookup skipped: IPINFO_LITE_TOKEN is not configured")
        return empty_geo()

    try:
        geo_debug(settings, "geo lookup requesting provider: ip=%s", ip)
        payload = await lookup(ip)
    except Exception:
        if settings.geo_lookup_debug:
            logger.exception("geo lookup failed for ip=%s", ip)
        return empty_geo()

    country = country_from_payload(payload)
    if not country:
        geo_debug(settings, "geo lookup response missing country code: payload=%s", payload)
        return empty_geo()
    geo_debug(settings, "geo lookup resolved from provider: country=%s", country)
    return {"country": country, "region": None, "city": None}


def geo_debug(settings: Settings, message: str, *args: Any) -> None:
    if settings.geo_lookup_debug:
        logger.info(message, *args)


def country_from_cloudflare(headers: Mapping[str, str]) -> str | None:
    country = headers.get("cf-ipcountry")
    if not country:
        return None
    country = country.strip().upper()
    if len(country) != 2 or country in {"XX", "T1"}:
        return None
    return country


def is_global_ip(ip: str) -> bool:
    try:
        return ip_address(ip).is_global
    except ValueError:
        return False


def ipinfo_lite_lookup(settings: Settings) -> GeoLookup | None:
    if not settings.ipinfo_lite_token:
        return None

    async def lookup(ip: str) -> Mapping[str, Any] | None:
        async with httpx.AsyncClient(timeout=settings.geo_lookup_timeout_seconds) as client:
            response = await client.get(
                f"https://api.ipinfo.io/lite/{ip}",
                params={"token": settings.ipinfo_lite_token},
            )
            response.raise_for_status()
            return response.json()

    return lookup


def country_from_payload(payload: Mapping[str, Any] | None) -> str | None:
    if not payload:
        return None
    country = payload.get("country_code") or payload.get("country")
    if not isinstance(country, str):
        return None
    country = country.strip().upper()
    if len(country) != 2:
        return None
    return country
