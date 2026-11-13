# Postgres backup to OCI Object Storage

### Postgres backup to OCI Object Storage (issue #106/#76, PR #122)

Self-hosted Postgres (2026-08-05 decision) had no backup safety net at all
until this shipped. Daily `pg_dump -Fc` → `oci os object put`, Celery beat
03:00 ET (`backup-database-daily`), 30-day retention enforced by the
bucket's Object Lifecycle Policy — not application code, to avoid a second
place for that number to drift.

- **Auth: OCI instance principal, not an API key.** Production runs the
  task on the app VM itself, so `--auth instance_principal` is added
  whenever `APP_ENV == "production"` (`_oci_auth_args()` in
  `app/services/db_backup.py`) — no OCI key file ever touches the server.
  Local/manual runs (e.g. a restore drill) fall back to the CLI's default
  `~/.oci/config`.
- **`pg_dump` and the `oci` CLI are invoked as subprocesses, never imported
  as Python libraries.** `oci`/`oci-cli` pin `cryptography<50.0.0`, which
  conflicts with this app's `cryptography==50.0.0` pin (Fernet holdings
  encryption, issue #31) and would silently downgrade it on install.
  `backend/Dockerfile`'s runtime stage installs `postgresql-client-16`
  (via the official PGDG apt repo, matching `postgres:16-alpine` exactly)
  and `oci-cli` in a fully isolated venv (`/opt/oci-cli`) — never in
  `requirements.txt`, never in the app's own `/venv`. Verified in the built
  image: app venv keeps `cryptography==50.0.0`, `/opt/oci-cli` has its own
  independent `cryptography==46.0.7`.
- **Production fails loud, not open, on misconfiguration.**
  `backup_database()` raises `BackupError` if `BACKUP_OCI_NAMESPACE` is
  unset (or whitespace-only) AND `APP_ENV == "production"` — this is the
  only DB restore safety net, so a missing/typo'd env var must not produce
  a daily "success" that backed up nothing. Local dev (namespace unset by
  default) silently no-ops instead, so an accidentally-started local Beat
  never uploads dev dumps anywhere.
- `backup_database_task` (`app/tasks/backup_tasks.py`) mirrors
  `capture_tasks.py`'s retry/ops-alert pattern (`max_retries=2` →
  `send_ops_alert` + GitHub issue on exhaustion), with one addition:
  `SoftTimeLimitExceeded` is caught separately and alerts immediately
  without retrying — a soft timeout at 920s means the attempt already
  burned nearly its full budget, so retrying would just delay the alert by
  up to two more ~920s attempts on a task billed as the only safety net.
  `time_limit`/`soft_time_limit` (960s/920s) are set above the sum of
  `db_backup.py`'s own subprocess timeouts (600s dump + 300s upload = 900s)
  — a soft/hard limit below that sum would fire mid-subprocess via signal,
  which `subprocess.run`'s own `timeout=` cleanup never sees, risking an
  orphaned `pg_dump`/`oci` child process.
- **Verified with two real restore drills** (issue #106 explicitly requires
  this — a backup script that's never been restored from is not a safety
  net): one locally against dev data, one against real production data
  entirely within the production server's boundary (no user data left the
  host). Both: dump → upload → download → `pg_restore` into a scratch
  database → row counts match the source exactly across all 8 tables →
  app-level Fernet decryption of holdings (via `SessionLocal` + `Holding`
  model pointed at the scratch DB) matches the source byte-for-byte,
  including CJK content. Full runbook + both drill writeups: Obsidian
  `Hermes/Portfonia/Portfonia Environment Config.md`.
- **Real production bug found during the production drill, not by either
  code-review round**: the OCI IAM policy scoping instance-principal access
  to the bucket was created via `oci iam policy create --statements
  '[...where target.bucket.name=\'portfonia-db-backups\'...]'` — bash's
  outer single-quote wrapping terminated early at the inner single quotes,
  silently submitting an **unquoted** string literal. OCI's policy engine
  accepts this with no error but the condition then never matches any real
  request — every object-storage call returned `BucketNotFound` (404),
  which reads exactly like a missing bucket, not a permissions gap, since
  OCI deliberately returns 404 rather than 403 for unauthorized access.
  Fixed via `oci iam policy update` with the value quoted, **verified by
  reading back the stored statement text** (`oci iam policy list --query
  .statements`) rather than trusting the update command's own success —
  full diagnostic writeup in Obsidian (same doc). Lesson generalizes beyond
  this project: never pass a CLI arg requiring nested quotes as an inline
  bash single-quoted string; use `file://` instead.
- **Provenance**: two rounds of independent code review (blacktomb42) on
  PR #122 — round 1 found 2 real bugs (a production instance name
  committed in a docstring, violating this repo's own infra-identifier
  policy; the pre-fix silent-skip-in-production behavior) + 4
  suggestions/nits, round 2 (after fixes) found 0 bugs + 4 suggestions/2
  nits, all verified against actual code and fixed. Both PENDING reviews
  submitted, resolution replies posted inline on each finding, plus a
  top-level PR comment per round.


### Container instance-principal auth depends on the IMDS SNAT source (issue #836)

Instance principal auth from inside a container works only while Docker's
SNAT sends metadata-service (169.254.169.254) traffic with the instance's
**primary** private IP. OCI IMDS answers 404 to requests from any other
source address, including a secondary IP on the same VNIC. The `oci` CLI
then fails with "Instance principal auth is only possible from OCI compute
instances", which hides the real cause.

- **Why the source can drift**: IMDS is reached through a direct link-local
  route with no gateway, so `MASQUERADE` picks the interface's first address.
  Ordinary egress goes through the subnet gateway and selects the primary
  address no matter how the addresses are ordered. After a secondary /32 was
  added to the host for the DERP relay, a `systemd-networkd` restart during
  OS patching (2026-10-26) re-applied the static address before the DHCP
  lease. Every backup upload failed from the next run (2026-10-27 03:00 ET)
  until the fix.
- **Fix (host configuration, not in this repo)**: a systemd unit
  `portfonia-imds-snat.service` (`PartOf=`/`WantedBy=docker.service`)
  inserts `nat POSTROUTING` rule 1:
  `-d 169.254.169.254/32 -o <nic> -j SNAT --to-source <primary IP>`. It is
  re-inserted on every Docker start, so Docker's own `MASQUERADE` rules
  cannot end up above it. The netfilter-persistent snapshot is deliberately
  left untouched, because it also holds Docker's rules. Actual values live
  in the Obsidian environment doc.
- **Rule**: any change to the host's addresses (adding or removing an IP,
  changing the primary IP) must keep this rule's `--to-source` in step. Then
  check IMDS from a container (`/opc/v2/identity/cert.pem` must return 200).
- **Diagnosis order for an IMDS 404**: compare the same request with
  `curl --interface <primary>` and `--interface <secondary>` on the host,
  then from inside a container. A 404 that carries IMDS's own response
  headers means the source address was rejected, not a missing path or an
  IAM policy (contrast the unquoted-policy `BucketNotFound` 404 above).

## Spend and host-resource observability

Issue #886 adds backend-only accounting and on-demand Ops reads. There is no
frontend change, scheduled spend report, or cost alert. Records contain metadata
only: no prompts, responses, holdings, report content, or email addresses.

### Storage and recording

One migration adds three tables:

- `llm_usage`: UUID, indexed `occurred_at`, nullable user FK (`ON DELETE SET NULL`),
  purpose, trigger source, requested model, nullable prompt/completion token counts,
  and nullable OpenRouter `usage.cost` as `numeric(12,6)` USD. User deletion retains
  spend while removing the user link.
- `external_call_daily`: composite ET date/provider primary key and a call counter.
- `host_samples`: UUID, indexed sample time, and the host measurements below.

`usage_recording.py` uses the independent operational-event sink engine and an
immediately committed, short-lived connection, never the business `Session`.
Database failures log one warning and return without changing the caller's outcome.
External counts use an atomic upsert for the ET day/provider pair.

Report Pass 2 uses `report_pass2`, analyze reruns use `report_regenerate`, render
translation uses `translation`, and upload parsing uses `holdings_parse`. Each
carries the owning user. Triggers are `scheduled` for Beat fan-out, `ops` for Ops
generation/reruns (inherited by translation), and `user` for uploads. Shared
`intel_classifier` and `intel_name_check` calls have no user and are never allocated
to users. OpenRouter requests retain their routing, reasoning and
`data_collection=deny` settings; usage inclusion supplies the reported cost.
Missing cost stays null. `_call_llm` records its final response only, excluding
responses discarded by its retry loop. Holdings parsing records each received
response; shared classification records before validating content, including
billed unusable responses. Existing `report_inputs.llm_calls`, intelligence
accounting/digests, and paid-search recording remain unchanged.

Each named outbound data request is counted, including retries, with one count per
yfinance fetching call regardless of batch size. Providers are `resend`, `finnhub`,
`massive`, `twelvedata`, `yfinance`, `eastmoney`, `sina`, `tencent`, `google_news`,
`sec`, `rss`, and `fred`. Direct Yahoo Search HTTP calls count as `yfinance`;
FRED requests count as `fred`. Only Resend sends have a per-call price:
USD 0.0009 (USD 0.90/1,000 emails). All other external counts cost zero.
Resend status GETs and `app/scripts/` calls are excluded. Tavily, Parallel and Exa
continue using `paid_api_usage`; OpenRouter uses `llm_usage`.

### Five-minute host sampling and alerts

`app.tasks.host_metrics_tasks.sample_host` runs under Beat as
`sample-host-every-5-minutes`, `crontab(minute="*/5")`, with API-quiet heavy
classification `False`. `host_metrics.py` reads host-level Linux files and commits
one row before finding the previous sample and evaluating alerts:

- `/proc/loadavg`: 15-minute load average.
- `/proc/meminfo`: total/available memory and total/free swap, converted from kB
  to bytes.
- `/proc/vmstat`: raw cumulative `pswpin`, `pswpout` pages and `oom_kill` count.
- `/proc/sys/vm/swappiness`: current swappiness.
- Root `statvfs`: used = `(f_blocks - f_bfree) * f_frsize`, available =
  `f_bavail * f_frsize`.
- Postgres: `pg_database_size(current_database())` bytes.
- Redis `INFO memory`: `used_memory` bytes, null on `RedisError`.

The reader exposes module-level `/proc` and statvfs paths for fixture tests.
Page size is 4096 bytes. Kernel counters stay raw; derived deltas skip negative
values after counter resets.

Alerts go to the admin mailbox for disk used/(used+available) >= 80%, memory
available/total < 10%, swap used/total >= 50%, swap-in >= 100 MiB since a previous
sample no more than 10 minutes old, a positive OOM-kill delta, or load >= 2.0.
Swap-in also requires a nonnegative counter delta. Each metric uses
`host-alert:{metric}:{ET date}` with existing `already_alerted`/`mark_alerted`;
only a successful `send_ops_alert(..., severity="ALERT")` marks the key, for two
days. This suppresses further successful alerts for that metric on that ET day.

### Ops reads and retention

Both endpoints inherit admin bearer authentication and router audit logging.
Dates are inclusive ET dates; reversed ranges return 422.

- `GET /admin/usage/costs?start=YYYY-MM-DD&end=YYYY-MM-DD`: defaults to today
  and the preceding six days. Returns `start`, `end`, and populated `days` only,
  ascending. Each day includes `llm_by_user` grouped by user/purpose/trigger
  (including purged null-user groups), `llm_shared` grouped by intel purpose,
  `paid_search` grouped by provider, and `external_calls` with calculated prices.
  LLM groups count calls with missing costs separately; totals sum known costs.
  Timestamp bucketing uses `America/New_York`; external daily dates are stored ET.
- `GET /admin/usage/host?start=YYYY-MM-DD&end=YYYY-MM-DD`: defaults both dates
  to today. Returns ascending `samples`, all stored columns, memory-available
  ratio, swap-used bytes/ratio, disk-used percentage, and `pswpin_delta`,
  `pswpout_delta`, `oom_kill_delta` relative to the preceding returned sample.
  First-row and negative deltas are null.

The existing daily `cleanup_operational_events` task also removes these three
record types older than 90 days, using bounded 1,000-row batches and their
`occurred_at`, `sampled_at`, or ET `day` fields. This does not change `user_purge.py`.
Production cost verification after deployment is a read of the two endpoints;
implementation tests mock OpenRouter, HTTP and Redis and make no paid calls.
