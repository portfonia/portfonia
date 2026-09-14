# Vigil runtime (P1.1 / P1.2 / P1.3)

Isolated Vigil compose project, database, and worker foundation. This is not a
Portfonia overlay: database name, Redis key/queue prefix, object bucket, KEK,
notification key, and Celery app are Vigil-specific. Do not import
`app.core.database` or register tasks on the Portfonia Celery app.

## Compose

Project name is `vigil`. Service, network, and volume names are Vigil-specific.

```bash
cp vigil/.env.example vigil/.env
# fill required VIGIL_* values; missing owner/key/database settings fail startup
docker compose --env-file vigil/.env -f vigil/compose.yml build
# production up is a separate owner authorization; do not arm dispatch/release
```

`docker compose down` against this file must not be used as a way to restart
Portfonia. The two compose projects do not share Postgres, Redis, or object
volumes.

## Shared reverse-proxy and Auth wiring (P1.2)

Public Caddy (`api.portfonia.com`) denies `/internal/*` with 404. Vigil talks
to Portfonia over a dedicated Docker network, not the public hostname.

One-time network provision (both compose files declare it `external: true`):

```bash
docker network create portfonia-vigil-internal
```

Only Portfonia `backend` and Vigil `vigil-backend` attach to that network.
Postgres, Redis, frontend, Caddy, and workers stay off it.

## Public reverse proxy (deploy follow-up to P1.3)

`vigil.portfonia.com` reaches Vigil's frontend through a second, separately
scoped external network — never `portfonia-vigil-internal`, which stays a
backend-to-backend-only channel:

```bash
docker network create portfonia-vigil-public
```

Only Portfonia `caddy` and Vigil `vigil-frontend` attach to
`portfonia-vigil-public`. Postgres, Redis, both backends, and Celery workers
stay off it. The public `Caddyfile` site block does a plain
`reverse_proxy vigil-frontend:3000` — no path-level rules, unlike
`api.portfonia.com`'s `/internal/*` deny.

Vigil settings for that hop:

- `VIGIL_PORTFONIA_INTERNAL_BASE_URL=http://backend:8000`
- `VIGIL_IDENTITY_SERVICE_TOKEN` must match Portfonia's
  `VIGIL_IDENTITY_SERVICE_TOKEN`
- `VIGIL_AUTH_ISSUER` is the hosted Auth issuer (`…/auth/v1`)

Management requests verify the JWT, allowlist `VIGIL_OWNER_AUTH_SUBJECT`, then
call Portfonia `GET /auth/session-status` on that internal URL. The scan path
calls only `GET /internal/vigil/principals/{auth_subject}` and never
session-status.

## Migrations

From `vigil/backend`, with `VIGIL_*` in the environment:

```bash
alembic upgrade head
alembic downgrade -1
```

The first revision creates `vaults`, `audit_events`, `runtime_heartbeat`, and
`consumed_nonces` only. Feature tables belong to later PRs.

## Readiness

`GET /health/ready` returns `{"status":"ready"}` (200) or
`{"status":"degraded"}` (503). It does not stamp `last_scan_completed_at`.
The registered scan task is inert until P3.3.

`GET /vault` returns a DISARMED no-object view without inserting a row.
A vault row is created only by `POST /vault`. The view includes `active_config_id`,
always-null `deadline_at` (until P3.3), and a nested `heartbeat` object.

The frontend is a Next.js app. It rewrites `/api/*` to `vigil-backend:8000` and
injects the host session bearer. Login is a popup to Portfonia `/auth/vigil`.
Public Caddy routing for `vigil.portfonia.com` is provisioned separately —
see "Public reverse proxy" above.

Dispatch, release, and arming stay disabled unless their flags are explicitly
enabled after later checkpoints land.
