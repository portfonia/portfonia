# Deployment (three-layer flow + env-only sync)

### Three-layer deployment flow (MANDATORY)

**Full workflow + production server specs (provider, region, instance name,
IP, SSH user, remote paths) live ONLY in Obsidian `Hermes/Portfonia/Portfonia Environment Config.md`
— never in this repo.** This file (`CLAUDE.md`) is git-tracked, so it must
never carry any traceable identifier for the production host: no IP, no
cloud provider/region, no instance name, no SSH username, no remote
filesystem path. Look those up in the Obsidian doc before running any of the
commands below — they're written here with the specifics deliberately
omitted.

The one hard rule that governs every action here: code authority is
**local → Git only**. Never edit code on the production server, never `git
commit` there, never use it as a sync hub between machines — its only
legitimate local state is `.env` (uploaded via `scp`).

**The free-tier spec ceiling drifts — re-verify before assuming a number.**
The provider has silently cut the free-tier allocation more than once
without notice. Don't hardcode a spec number from memory or from an old note
here — check the provider's console/CLI before planning capacity.

**SSH stays open, guarded by fail2ban only** (`maxretry=10`, `findtime=10m`,
`bantime=10m` — relaxed from defaults after a prior fail2ban lockout on a
different service cost hours to recover from serial console). No source-IP
restriction: the dev machine has no fixed IP, and an agent session's own
egress IP isn't stable across runs either. If a future session gets banned
mid-task, the ban self-clears in 10 minutes — don't burn time trying to
route around it via the provider's serial console unless the task can't
wait.

**The dev-machine → production path is unreliable — not the production
server itself.** The production server has no known network problems on its
own connection to the internet or to OpenRouter. What's flaky is
specifically the link from the local dev machine to it, which routes
through the user's VPN/TUN proxy (confirmed 2026-08-06: SSH from this
machine repeatedly dropped mid-command while the server's own load/network
were fine). This means: **the connection can drop mid-command with no
warning** for anything originating from the dev machine (Claude Code's own
SSH, and likely the user's browser too, if it routes through the same
proxy) — two separate `docker compose up --build` launches died silently
mid-build this way, one via `nohup ... & disown` on the remote side, one via
keeping the SSH session itself alive locally with `run_in_background` —
neither survives an actual network drop, because both still depend on the
TCP/SSH connection staying up long enough to hand off. Don't extrapolate
this to "the production site is unreliable for real users" — a real user
connecting independently over the open internet doesn't go through this
proxy path. **For any remote command expected to run longer than a few
seconds, use `systemd-run` on the server** so the command runs as a
transient unit fully independent of the SSH session — get the exact
host/user/path from the Obsidian doc, then:

```bash
ssh <host-from-obsidian-doc> "sudo systemd-run --unit=portfonia-deploy --working-directory=<path-from-obsidian-doc> -- docker compose up -d --build"
# reconnect any time after, even following a dropped connection, to check on it:
ssh <host-from-obsidian-doc> "systemctl status portfonia-deploy; sudo journalctl -u portfonia-deploy --no-pager"
```

**Watching is the same problem as launching.** After `systemd-run`, drop
the SSH session. Check progress with short reconnects (`systemctl
is-active` / `systemctl show` / `tail` of an on-server log). Do not hold
`ssh '... while systemctl is-active ...'`, a local monitor whose child is
a long-lived SSH, or `run_in_background` on an SSH that stays connected
for the job — those die with the VPN/TUN drop the same way a foreground
`docker compose up` does. A dropped check is not task failure; reconnect
and read the unit and the log. Redirect the job's stdout to a file on the
server when `journalctl` will not carry the full stream (interactive
Python, `docker compose exec`). This applies to every long production
command (deploy, UAT, one-shot `docker compose exec`), not only
`portfonia-deploy`.

Do not trust a `nohup`/`disown`/backgrounded-SSH exit code as proof a long
remote command finished — verify by checking the actual resulting state
(containers running, files present), not just the shell's reported exit
status.

**An explicit, unambiguous request to deploy the currently-merged `main` to
production — in whatever language or phrasing the requester uses — means
execute this procedure** (established 2026-08-06, after the first
successful full-stack deploy). The human workflow ends at PR merge to
`main` (branch → implement → test → PR → review → fix → merge, all local);
production deployment is the one additional step that ships a merged `main`
to the production server:

1. Sanity-check local `main` is clean and matches `origin/main` (don't
   deploy stale/uncommitted state).
2. SSH in (host/user/path from the Obsidian doc), `git pull`.
3. `sudo systemd-run --unit=portfonia-deploy --working-directory=<path> -- docker compose up -d --build`
   — always systemd-run, never a plain foreground/backgrounded SSH command,
   regardless of how small the change looks (a `--build` with no
   dependency changes is fast due to layer caching, but the connection can
   still drop mid-command).
4. Poll for completion, tolerating transient SSH check failures (retry the
   check, don't treat a dropped check-connection as deploy failure) but
   treating an actual `exited (1/2/137/139)` container or a `failed`
   systemd unit as real failure.

   **Use this exact polling script, run locally with `run_in_background`
   (not `Monitor` — this needs exactly one completion notification, not a
   per-line event stream; not a bare foreground `sleep` loop either, the
   harness blocks chained `sleep`s outside `run_in_background`/`Monitor`).
   Do not write a fresh one from scratch** — two hand-rolled attempts have
   already broken on this exact script (both on 2026-08-26, ~13 hours
   apart): first, one that checked `systemctl show -p SubState --value`
   against the literal string `exited`, which a `systemd-run` transient
   unit never actually reaches on success (it goes `running` → `dead`, not
   `exited` — that value applies to oneshot units with `RemainAfterExit`,
   not this unit type) — the loop would have spun forever if not caught and
   killed manually mid-task. Second, a corrected version that named its
   loop variable `status` — the dev machine's default shell is zsh (per
   this environment's own shell setting), where `status` is a builtin
   read-only variable aliased to `$?`; the assignment fails at the
   interpreter level (`read-only variable: status`) and the script exits
   immediately with no useful signal about the deploy itself. Use
   `unit_state`, not `status`, and don't reuse this pitfall on any future
   variant of this script:

   ```bash
   while true; do
     unit_state=$(ssh -o ConnectTimeout=15 -i <key-from-obsidian-doc> \
       <user>@<host> "systemctl is-active portfonia-deploy" 2>/dev/null)
     ssh_exit=$?
     if [ "$ssh_exit" -eq 255 ]; then
       # SSH connection itself failed (the known VPN/TUN drop) — retry,
       # this is not a signal about the unit's state either way.
       sleep 15
       continue
     fi
     if [ "$unit_state" != "active" ] && [ "$unit_state" != "activating" ]; then
       # Unit left the running states — could be "inactive"/"dead" (done,
       # check Result next) or "failed". Either way, stop polling.
       break
     fi
     sleep 15
   done
   ssh -o ConnectTimeout=15 -i <key> <user>@<host> \
     "systemctl show portfonia-deploy -p Result,ExecMainStatus --value"
   # Result=success -> proceed to step 5. Anything else -> real failure,
   # pull the journal (see the systemd-run/journalctl commands above).
   ```

   The `ssh_exit -eq 255` branch is the part that's easy to get wrong: a
   plain `until ! ssh ... ; do sleep; done` treats a dropped SSH connection
   (exit 255) the same as "the remote command ran and said no" — since `!`
   inverts a non-zero exit to true either way, a transient network drop
   would look identical to "the unit finished" and end the loop early on a
   false positive. Distinguishing the two is why this script checks
   `$ssh_exit` before looking at `$unit_state` at all.
5. `curl https://api.portfonia.com/health` — confirm `{"status":"ok",...}`.
6. `docker builder prune -af` — reclaim BuildKit's build-cache layers left
   behind by this deploy's `--build` (and every prior one). Safe after a
   successful deploy: prunes only cache, never touches the images just
   tagged/running, containers, or volumes — verify with `docker compose ps`
   (all services still Up) and `docker system df` (Build Cache back near
   0B) if in doubt. **Do this every deploy, not just when disk looks
   tight**: discovered 2026-09-05 that this had never been run since the
   server went live — build cache had silently grown to 29GB (28GB
   reclaimable), pushing `/` to 80% used, while the actual running images
   totaled under 3GB. A production host has no local dev workflow to
   surface this the way Colima's own disk-pressure errors do locally, so
   skipping this step lets it grow unnoticed until disk actually runs out.
7. Report success (what changed) or failure (which step, what the logs
   showed) — don't declare done without step 5 passing.

**Before step 3, check whether the commits being deployed add a new
required `Settings` field** (`app/core/config.py` — no default, not
`| None`). If so, that value must already be in the server's `.env` (or be
added there — a fresh key, never copied from `.env.local`'s dev value)
*before* `docker compose up -d --build` runs, or `migrate`/`backend`/
`celery-worker`/`celery-beat` will all fail Pydantic validation at
container start. `docker-compose.yml`'s `migrate` service (one-shot
`alembic upgrade head`, gated by `depends_on: postgres:
service_healthy`) runs before `backend`/`celery-worker`/`celery-beat`
via `condition: service_completed_successfully` — so a missing required
var fails cleanly (migrate exits non-zero, dependents never start) rather
than partially starting, but it's still a failed deploy. Confirmed this
gate actually encrypts real production rows correctly (issue #31 deploy,
2026-08-09): `docker compose exec backend python -c "..."` reading
`Holding` rows through the live app process, not just `/health`, is the
right depth of check when a migration transforms existing data — a green
`/health` alone doesn't prove the migration ran or that decryption works
against real rows.

**`systemd-run --unit=portfonia-deploy` fails with "Unit ... was already
loaded or has a fragment file" if a previous attempt's unit is still
registered in a `failed` state** (systemd doesn't auto-clean failed
transient units — only successful ones vanish). Hit this 2026-08-09 from a
stale unit left over from the unrelated 2026-08-07 `frontend/public/`
build failure (issue #100/#101, long since fixed). Fix: `sudo systemctl
reset-failed portfonia-deploy` before retrying `systemd-run` with the same
unit name — don't rename the unit to dodge this, the reset is one command
and keeps the naming convention stable for the next session's `systemctl
status portfonia-deploy` check.

**A second, unrelated instance exists in the same cloud tenancy — never
touch it** (stop/resize/reconfigure/reuse) when working on Portfonia infra.
It belongs to a different project and sits in its own isolated network. See
the Obsidian doc to identify it if you need to confirm you're not touching
it.


### Env-only sync to production (no code change involved)

**An explicit request to push `.env` changes (secret rotation, config value
change) to the server without an accompanying code change is a separate,
smaller procedure from the code-deploy flow above** — established
2026-08-06, first used to roll out a rotated Resend key:

1. **Before overwriting**, diff key *names* (never values) between the
   server's current `.env` and local `.env.production` — `ssh ... "grep -oE
   '^[A-Z_]+=' .env"` vs `grep -oE '^[A-Z_]+=' .env.production`, both piped
   to `sort -u` and `comm`'d. A var that only ever exists on the server side
   (generated directly during a prior deploy and never echoed back to local
   — e.g. `ADMIN_API_TOKEN`, generated on-server 2026-08-23 for B2) will be
   **silently dropped** by a blind `scp` overwrite, since `scp` replaces the
   whole file rather than merging. If the diff finds one, recover the live
   value from a still-running container's process env
   (`docker compose exec -T <service> printenv <VAR>`) **before** the deploy
   step recreates that container, and append it back to the server `.env`
   before proceeding — don't rely on remembering to check afterward (caught
   2026-08-24 during the B4+B5 deploy: `ADMIN_API_TOKEN` would have been
   lost this way had the diff not been run first).
2. `scp` the local `.env.production` to the server's `.env` (path from the
   Obsidian doc) — `git pull` is irrelevant here, `.env` never travels
   through Git.
3. **`docker compose restart <service>` does NOT reload `env_file` values**
   — Compose only re-reads `env_file` when a container is *recreated*, not
   on a plain restart of an existing one. Recreate explicitly:
   `docker compose up -d --force-recreate <services>` — target only the
   services that actually declare `env_file: .env` in `docker-compose.yml`
   (currently `backend`, `celery-worker`, `celery-beat`; `frontend`/`caddy`
   don't and shouldn't be touched for an env-only change — minimal blast
   radius).
4. If the code-deploy procedure above (`docker compose up -d --build`) is
   running concurrently on the server, wait for it to finish before doing
   this — both operate on the same `docker compose` project and can race
   (`--force-recreate` on the same containers a build is replacing).
5. Verify: `curl https://api.portfonia.com/health`, then a real functional
   check of whatever changed (e.g. for an email-provider key rotation, exec
   into the `backend` container and send a real test message through the
   actual send path — don't just trust a 0 exit code from a function that
   swallows its own exceptions and logs to a stream `docker compose exec`
   won't show you; print the provider's raw HTTP response instead).


