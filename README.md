# Analytics Collector

FastAPI service for collecting leaderboard analytics events.

## Purpose

This service is the public browser-facing analytics collector for the leaderboard frontend.
The frontend sends anonymous event batches here; this service validates, enriches, rate-limits,
and writes them to MongoDB.

Browsers must never connect to MongoDB directly.

## Run Locally

```powershell
uv sync
uv run uvicorn analytics_collector.app:app --reload --host 0.0.0.0 --port 9000
```

Health check:

```powershell
Invoke-RestMethod http://localhost:9000/health
```

## Environment

Copy `.env.example` to `.env`.

| Variable | Default | Description |
| --- | --- | --- |
| `ALLOWED_ORIGINS` | `[]` | Origins allowed to write events. Use JSON list syntax, e.g. `["https://example.com"]`. Empty list disables origin enforcement and is not recommended for production. |
| `ANALYTICS_SITE_ID` | unset | Optional site id. When set, requests must include `X-Analytics-Site-Id`. This is a weak public gate, not a secret. |
| `RATE_LIMIT_PER_MINUTE` | `60` | Max event batches per minute per `ip + visitorId + sessionId`. |
| `MONGO_URL` | `mongodb://localhost:27017` | MongoDB connection string. |
| `MONGO_DATABASE` | `analytics` | MongoDB database name. |
| `MONGO_COLLECTION` | `analytics_events` | Raw event collection name. |
| `IP_HASH_SALT` | `change-me` | Salt used to hash client IPs before storage. Change in production. |
| `IPINFO_LITE_TOKEN` | unset | Optional IPinfo Lite token. When set, public client IPs are resolved to country-level geo after `CF-IPCountry` fallback. |
| `GEO_LOOKUP_TIMEOUT_SECONDS` | `1.0` | Timeout for IPinfo Lite country lookup. Lookup failures do not reject analytics events. |
| `GEO_LOOKUP_DEBUG` | `false` | When true, emits geo lookup decision logs to help local debugging. Logs may include client IPs; keep disabled in production. |

## API

### `GET /health`

Returns:

```json
{ "status": "ok" }
```

### `POST /v1/events/batch`

Headers:

- `Origin`: must match `ALLOWED_ORIGINS` when configured.
- `Referer`: accepted as a fallback origin source.
- `X-Analytics-Site-Id`: required only when `ANALYTICS_SITE_ID` is configured.
- `CF-IPCountry`: optional country-code shortcut when provided by the deployment platform/CDN.

Request:

```json
{
	"visitorId": "visitor-1",
	"sessionId": "session-1",
	"events": [
		{
			"id": "evt-1",
			"eventName": "page_view",
			"sentAt": "2026-06-23T01:00:00.000Z",
			"page": {
				"path": "/models",
				"queryKeys": ["q"]
			},
			"payload": {
				"path": "/models",
				"queryKeys": ["q"]
			}
		}
	]
}
```

Response:

```json
{ "accepted": 1 }
```

Supported `eventName` values:

- `page_view`
- `search_changed`
- `filter_changed`
- `sort_changed`
- `tab_selected`
- `model_pinned`
- `model_unpinned`
- `compare_opened`
- `compare_model_changed`
- `csv_downloaded`
- `share_link_copied`
- `external_link_clicked`

## Anti-Abuse Strategy

This endpoint is public because browsers need to call it. The collector therefore assumes
requests can be forged and uses defensive controls instead of hidden credentials.

- Reject requests whose `Origin` or `Referer` does not match `ALLOWED_ORIGINS`.
- Optionally require `X-Analytics-Site-Id` as a weak public gate.
- Validate event names and reject unknown fields in the event envelope.
- Limit each batch to 50 events and cap string/list lengths through Pydantic models.
- Rate-limit by `ip + visitorId + sessionId`.
- Add server-side fields and trust metadata; dashboards should query trusted/aggregated data.

Stored trust object:

```json
{
	"originOk": true,
	"siteIdOk": true,
	"schemaOk": true,
	"rateLimited": false,
	"source": "browser"
}
```

## Stored Event Shape

Each accepted frontend event is expanded into one MongoDB document:

```json
{
	"visitorId": "visitor-1",
	"sessionId": "session-1",
	"id": "evt-1",
	"eventName": "page_view",
	"sentAt": "2026-06-23T01:00:00.000Z",
	"receivedAt": "2026-06-23T01:00:01.000Z",
	"page": { "path": "/models", "queryKeys": ["q"] },
	"payload": { "path": "/models", "queryKeys": ["q"] },
	"ipHash": "sha256...",
	"userAgent": "browser user-agent",
	"referer": "https://example.com/",
	"origin": "https://example.com",
	"geo": { "country": "US", "region": null, "city": null },
	"trust": {
		"originOk": true,
		"siteIdOk": true,
		"schemaOk": true,
		"rateLimited": false,
		"source": "browser"
	}
}
```

`geo.country` is stored as an ISO 3166-1 alpha-2 country code when available.
The collector first trusts a valid `CF-IPCountry` header. If that header is absent and
`IPINFO_LITE_TOKEN` is configured, it calls IPinfo Lite for public client IPs only.
Local, private, reserved, unknown, and lookup-failure cases keep `geo` values as `null`.
Client IP extraction uses `x-forwarded-for` first, then `x-real-ip`, then
`cf-connecting-ip`, then the socket client host. Header IPs with ports are normalized
before geo lookup.

For local geo debugging, set `GEO_LOOKUP_DEBUG=true` and watch the server logs. The
collector will log whether it used `CF-IPCountry`, skipped a non-public IP, skipped
because `IPINFO_LITE_TOKEN` is missing, called IPinfo Lite, or received an unusable
provider response.

## MongoDB Indexes

Suggested raw event indexes:

```javascript
db.analytics_events.createIndex({ receivedAt: -1 });
db.analytics_events.createIndex({ visitorId: 1, receivedAt: -1 });
db.analytics_events.createIndex({ sessionId: 1, receivedAt: -1 });
db.analytics_events.createIndex({ eventName: 1, receivedAt: -1 });
db.analytics_events.createIndex({ "page.path": 1, receivedAt: -1 });
db.analytics_events.createIndex({ "geo.country": 1, receivedAt: -1 });
```

For dashboards, build trusted daily/hourly aggregates into a separate collection such as
`analytics_daily_metrics` rather than scanning raw events for every request.

## Test

```powershell
uv run pytest -q
```
