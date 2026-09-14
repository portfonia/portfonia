# Vigil runtime (P1.1)

Isolated Vigil compose project, database, and worker foundation. This is not a
Portfonia overlay: database name, Redis key/queue prefix, object bucket, KEK,
notification key, and Celery app are Vigil-specific. Do not import
`app.core.database` or register tasks on the Portfonia Celery app.

## Compose

Project name is `vigil`. Service, network, and volume names are Vigil-specific.
Shared reverse-proxy / Auth wiring is out of this checkpoint.

```bash
cp vigil/.env.example vigil/.env
# fill required VIGIL_* values; missing owner/key/database settings fail startup
docker compose --env-file vigil/.env -f vigil/compose.yml build
# production up is a separate owner authorization; do not arm dispatch/release
```

`docker compose down` against this file must not be used as a way to restart
Portfonia. The two compose projects do not share networks or volumes.

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

Dispatch, release, and arming stay disabled unless their flags are explicitly
enabled after later checkpoints land.
