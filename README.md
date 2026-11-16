# Enterprise Risk Bot: Learning Baseline

A deliberately minimal Microsoft Teams bot backend built with FastAPI and the
Microsoft Bot Framework Python SDK. This baseline exists to make the inbound
Teams activity lifecycle easy to understand before business features are added
back one at a time.

## Runtime responsibilities

The application currently does only four things:

1. starts FastAPI;
2. creates an authenticated Bot Framework `CloudAdapter`;
3. accepts Bot Framework activities at `POST /api/messages`;
4. delegates activities to a minimal `TeamsActivityHandler`.

The internal architecture includes MongoDB-backed installation/channel/routing
state, notification and reaction services, safe activity diagnostics, and
retry-safe activity idempotency. These capabilities remain internal to the bot
activity flow, plus the read-only installation inventory API.

## Request flow

```text
Microsoft Teams / Azure Bot Service
                ↓
        POST /api/messages
                ↓
       deserialize Activity
                ↓
      read Authorization header
                ↓
 CloudAdapter.process_activity(...)
                ↓
     RiskBot → SDK activity dispatch
```

`CloudAdapter` authentication remains enabled. The application never logs the
Authorization header.

## Application routes

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Process liveness |
| `GET` | `/ready` | Minimal application readiness |
| `POST` | `/api/messages` | Authenticated Bot Framework activities |
| `GET` | `/api/installations` | Read-only Teams installation inventory |
| `POST` | `/api/destinations/resolve` | Explicitly configure one discovered channel as a notification destination |
| `DELETE` | `/api/destinations/{destination_id}` | Disable one notification destination without uninstalling the Team app |

FastAPI documentation remains available at `/docs`, `/redoc`, and
`/openapi.json`.

## Source structure

```text
src/
├── main.py
├── application.py
├── api/
│   ├── bot_routes.py
│   ├── dependencies.py
│   └── health_routes.py
├── bot/
│   ├── adapter.py
│   └── handlers/
│       └── risk_bot.py
├── config/
│   └── settings.py
├── middleware/
│   └── request_logging.py
├── schemas/
│   └── health.py
├── utils/
│   └── logger.py
└── exceptions.py
```

## Configuration

Copy the example environment file and provide the values from the existing
Microsoft Entra/Azure Bot registration:

```bash
cp .env.example .env
```

| Variable | Purpose |
|---|---|
| `MICROSOFT_APP_ID` | Bot application ID |
| `MICROSOFT_APP_PASSWORD` | Bot client secret |
| `MICROSOFT_APP_TENANT_ID` | Single-tenant Entra tenant ID |
| `MICROSOFT_APP_TYPE` | Expected to be `SingleTenant` |
| `LOG_LEVEL` | Application logging level |
| `CORS_ALLOWED_ORIGINS` | Comma-separated trusted browser origins; defaults to the two localhost:3000 origins |

The Azure Bot messaging endpoint remains:

```text
https://YOUR_PUBLIC_HOST/api/messages
```

Do not place secrets in source control.

## Teams installation and destination semantics

Teams installs the app at **Team scope**.  StratSync therefore stores exactly
one installation per `(tenant_id, team_id)` and never treats a standard channel
as a separate app installation.

- Channel discovery stores available channels only; it does not create
  destinations.
- A notification destination is created only through
  `POST /api/destinations/resolve` for a selected discovered channel.
- `GET /api/installations` distinguishes the states for a destinations UI:
  `installation_status_label: "Active"`, and for each channel either
  `status_label: "Available"` or `status_label: "Configured"`.
  `is_configured` is the boolean equivalent; the older `connected` field is
  retained for compatibility.
- Notification sends require an active configured destination.  Discovery or
  a Team installation alone is insufficient.
- Removing the Team app soft-deactivates that Team's destinations.  Removing
  a destination only disables that destination (`is_configured: false`) and
  leaves the Team app installed.

## Docker-first workflow

```bash
docker compose build backend
docker compose run --rm backend python -m compileall src
docker compose run --rm backend pytest -q
docker compose up -d --build
```

The production command is:

```bash
uvicorn src.main:app --host 0.0.0.0 --port 3978
```

Check the running application:

```bash
curl -s http://localhost:3978/health
curl -s http://localhost:3978/ready
```

Expected responses:

```json
{"status":"ok"}
```

```json
{"status":"ready"}
```

The previous Mongo volume is intentionally not referenced by this baseline.
Do not run `docker compose down -v`; historical data remains available for a
later learning step.

## Risk timing and hourly card updates

Alert creators must supply `reported_date` as an ISO-8601 timestamp with an
offset and `reported_timezone` as an IANA timezone when creating a risk. The
backend stores the timestamp as UTC and recalculates the displayed elapsed time
whenever it renders a notification, Details, or Mitigation card. Client-sent
`elapsed_time` is ignored.

## Hourly card updates

The opt-in `hourly-card-updater` Compose profile refreshes eligible, bot-authored
notification cards at each completed UTC hour. It persists the exact returned
activity ID plus a sanitized conversation reference only after the original send
succeeds. The worker uses the same authenticated CloudAdapter configuration as
the API, starts a proactive conversation, and calls Bot Framework
`TurnContext.update_activity`; it never posts a replacement message.

Enable it explicitly:

```bash
docker compose --profile hourly-updates up -d --build
```

Each run takes a Mongo-backed lease, atomically claims each
`notification_id:hour-checkpoint`, and records success, retryable failures, or
terminal inaccessible-reference failures. Legacy records without a complete
message reference are skipped. The worker processes only the current checkpoint
after a restart, so it does not replay stale hourly content.

### Manual Teams verification

1. Resolve a channel destination and send a new risk notification.
2. Confirm the notification record has `status: sent`, a `teams_message_id`,
   and `update_eligible: true`; never add these fields manually.
3. Enable the Compose profile, wait for the next UTC hour, and confirm the same
   Teams message updates its elapsed time while retaining its activity ID,
   Actions, native tables, and full-width card layout.
4. Confirm no second channel message was created. Disable the profile after the
   test with `docker compose --profile hourly-updates stop hourly-card-updater`.

### Rollback and troubleshooting

Disable the worker profile (or set `HOURLY_CARD_UPDATE_WORKER_ENABLED=false`) to
stop future updates without changing sent cards or deleting notification data.
Inspect `update_state`, `latest_update_error`, and `failure_count` on the
notification record. Authorization, deleted-message, and invalid-reference
failures become terminal/inactive; transient failures retain a backoff timestamp
for retry. Live Teams behavior remains an integration verification step because
host permissions and tenant policy cannot be proven by mocked unit tests.

## Internal persistence

MongoDB is started by Docker Compose with the named `mongodb_data` volume.
Application collections are `teams_installations`,
`teams_discovered_channels`, `destinations`, `notifications`,
`notification_reactions`, and `processed_activities`. Do not run
`docker compose down -v`; existing Mongo data must remain recoverable.

Set `TEAMS_DEBUG_ACTIVITY_LOGGING=true` only for development diagnostics. Raw
activities are sanitized before debug logging, and Authorization headers and
credentials are never logged.

## Tests

The focused test suite verifies:

- health and readiness responses;
- the exact application route inventory;
- `/api/messages` Activity deserialization and adapter delegation;
- lifespan wiring of `CloudAdapter` and `RiskBot`;
- delegation from `RiskBot` to SDK activity dispatch.

No test fakes Microsoft authentication as an application feature. The route
passes the real Authorization header to `CloudAdapter.process_activity`, where
the Bot Framework authentication pipeline handles it in production.
