# Subscription core and lifecycle (issues #595, #596, #600, #610, #650, #660 and #710)

This backend implements user subscription operations and the scheduled
lifecycle. Profile provides quote-backed plan, cancel and resume controls in #597,
with Welcome guidance and public billing copy; cash purge/adjustments
belong to #599. These issues are deployed together. No production operation is part
of this implementation.

## State and ownership

`app/services/subscription.py` owns subscription state and `report_cadence`,
apart from signup's initial `none`. `app/services/credit_ledger.py` remains the
only balance and ledger writer. The migration `s59500000001` adds:

| User column | Type / default | Meaning |
|---|---|---|
| `subscription_status` | Non-null text, `inactive` | `active`, `expired`, `cancelled`, `inactive` |
| `subscription_type` | Nullable text | `weekly`, `mwf`, `daily` or `jade` |
| `subscription_expires_on` | Nullable date | Last valid ET day of the paid period |
| `subscription_period_start` | Nullable date | First paid day and charge-key date |
| `subscription_anchor_day` | Nullable smallint, 1..31 | Calendar-month anchor |
| `subscription_cancel_pending` | Non-null boolean, false | Cancel requested during a paid period |
| `subscription_adjusted_on` | Nullable date | Last user adjustment's ET date |

New signups are inactive with cadence `none`. Migration preserves every
existing cadence and gives existing rows inactive subscription defaults.
The post-activation invariant is: active/expired briefing subscriptions have
cadence equal to type; active/expired Jade subscriptions have cadence `weekly`,
`mwf` or `daily`; inactive/cancelled subscriptions have null type and cadence
`none`. Legacy rows acquire this invariant when the owner runs launch activation.
Scheduled fan-out requires active subscription status.

## Billing and transactions

`PLAN_FEES` is Weekly 0.99, Mon/Wed/Fri 1.99, Daily 2.49 and Jade 9.99
credits/month.
Fees are read at charge time. `next_expiry` selects the following calendar
month with the anchor day clamped to its last day: 2027-01-31/31 gives
2027-02-28, then 2027-02-28/31 gives 2027-03-31. Expiry is inclusive;
overdue means `today_et() > subscription_expires_on`.

Every user operation locks and refreshes the user row using `FOR UPDATE`
and `populate_existing=True`. An adjustment already made today raises
`daily_limit`; each successful operation records today. Ledger calls happen
before subscription edits, followed by flush; routers commit. Failed
operations roll back. This ordering prevents ledger refresh from discarding
unflushed subscription edits. A daily lock ends at the next ET midnight.

`set_plan` requires a verified account or delivery email. Its branches are:

1. Active, same plan, no pending cancellation: `no_change`, even if overdue.
2. Active, same plan, pending cancellation, not overdue: resume without charge.
3. Active, different plan, not overdue: return unused credits and charge a new
   full month from today.
4. Otherwise: subscribe afresh without a return, including an overdue active
   subscription with a different plan or pending cancellation.

For a change, read the original `subscription:{user_id}:{period_start}:{plan}`
ledger rows. Let `paid` be the absolute total, `total_days = expiry - start + 1`
and `unused_days = expiry - today + 1`. The change day counts as unused:

```text
returned = min(paid, round_up_to_cent(paid * unused_days / total_days))
cash_return = min(returned, cash_paid)
gift_return = returned - cash_return
```

Check sufficiency after the return before writing anything. Post the return
with `subscription_return:{old_charge_key}`, then consume the new full fee
(gift first, cash second). Returns never exceed the original payment and
never convert gift into cash. Fresh subscription sets active status, type
and cadence, today's period start/anchor, next expiry, and clears cancellation.

Example: gift 5.00, subscribe Weekly on 2026-10-15 -> gift debit 0.99,
balance 4.01, expiry 2026-11-15. Change to Mon/Wed/Fri on 2026-10-31:
16 unused days / 32 total days returns 0.50 gift, charges 1.99, leaves 2.52,
and sets expiry 2026-11-30. If the original charge used gift 0.50 and cash
1.49, a 1.93 return gives cash 1.49 and gift 0.44.

## Cancel, resume and unsubscribe

Cancel requires active status and no pending cancellation. It sets the flag
without changing type, cadence, expiry or ledger. Resume requires active,
pending cancellation, a non-overdue period and a verified email; it clears
the flag without charging. Selecting the original plan before expiry also
resumes. Error codes are `no_subscription`, `no_change`, `email_unverified`
and the common `daily_limit`.

Report-email unsubscribe still clears the matching verification timestamp
and immediately stops delivery to that address. If no verified address
remains, `cancel_for_no_verified_email` marks an active subscription pending
cancellation. This system action ignores and does not consume the daily
adjustment lock. Another verified address prevents cancellation. Re-verifying
an email does not automatically resume a cancelled subscription. The scheduled check transitions to cancelled after expiry.

## API and integrations

All subscription endpoints use `current_principal`:

| Endpoint | Result |
|---|---|
| `GET /me` | Adds `subscription`: status, cadence, nullable type/expiry, cancel flag, nullable next adjustment datetime |
| `GET /me/subscription/quote?type=weekly\|mwf\|daily\|jade` | Read-only quote; no user lock or writes |
| `POST /me/subscription` with `{"type":"weekly"}` or `{"type":"mwf"}` or `{"type":"daily"}` or `{"type":"jade"}` | Subscribe, change or same-plan resume; returns subscription summary |
| `POST /me/subscription/cancel` | Cancel pending; returns summary |
| `POST /me/subscription/resume` | Resume; returns summary |

User-operation errors return HTTP 409 with the service code in `detail`;
invalid plan input returns 422. No additional rate limiter is introduced.
Amounts in quotes are two-decimal strings; dates are ISO dates and timestamps
are ET ISO datetimes. `next_adjustment_at` is next ET midnight only when an
adjustment was made today. Expiry remains available for expired/cancelled display.

Quotes share plan selection and return arithmetic with writes. They expose
`action` (`subscribe`, `change`, `resume`, `none`), type, fee, returned, balance,
balance after, sufficient, period start, expiry, first report time,
cadence, `needs_holdings`, and nullable `blocked` (`daily_limit` or `email_unverified`).
Numbers remain present when blocked. `needs_holdings` warns for Mon/Wed/Fri
and Daily with zero holdings, without refusing the subscription.

Ops directory rows gain status, type, expiry and cancellation flag; its cadence
filter accepts `none`. The existing Ops cadence route returns 409
`cadence changes are suspended; cadence follows the subscription` without
reading/writing the user. Portfolio overview emails omit the next-report line
when cadence is `none`.

`next_occurrence_for_cadence` reads fire times only from `_REPORT_CADENCES`.
Issue #600 corrects displayed times by adding Celery's absolute remaining
duration in UTC, then converting back to ET. No offset, timezone-transition
rule or date is hard-coded; The same helper reads Daily weekday and existing cadence definitions.
For the quote at 2026-10-31 noon ET, the next Mon/Wed/Fri report is
2026-11-02 at 17:00 ET.

## Daily and Advanced access (issue #650)

Daily costs 2.49 credits per calendar-month period and sends at 17:00 ET
Monday through Friday, including NYSE holidays, with the same content as
Mon/Wed/Fri. Both cadences require at least one holding for dispatch. Weekly
still sends Saturday at 19:00 ET and permits an empty book. Daily uses
`session_node="daily_close"`; Mon/Wed/Fri uses `after_close`, and Weekly uses
`weekend_snapshot`. Report history labels Daily separately.

`ADVANCED_SUBSCRIPTION_TYPES = ("daily", "jade")` and `is_advanced(user)` are the
single definition of Advanced access: active subscription status and membership
in that tuple. A cancel-pending active Daily or Jade subscription remains Advanced
until lifecycle expiry is processed. Switching to Weekly or Mon/Wed/Fri removes
snapshot access immediately; expired, cancelled and inactive subscriptions have
no Advanced access. `GET /auth/session-status` returns HTTP 200 with
`{"advanced": bool, "jade": bool}` from the already-loaded principal; invalid sessions still
return 401. The authenticated Daily user's Get started trigger is gold; Jade takes
precedence with its green texture.

Issue #660: a successful Profile subscribe, change, cancel or resume calls
`revalidateSession()` after `router.refresh()`, so the trigger re-probes
`session-status` and turns gold or back to default in the same interaction.
A failed write does not re-probe. Ends that happen without a Profile action
(cancel-pending Daily reaching expiry, renewal falling to `expired`) are
picked up by the existing mount/focus/visibility re-verification; there is
no polling timer. `useSubscription` stores errors as a translation key plus
values and translates at render, so a UI language switch re-translates the
daily-lock notice with the countdown frozen at the click. Outside the
confirmation dialog the notice renders directly below the plan select in
Report management. In Simplified and Traditional Chinese the `mwf` plan is
named "every other day" (plan title, plan select, report-history type,
public copy and the subscription notice emails), with the Monday/Wednesday/
Friday send days kept wherever copy describes delivery; English keeps
"Mon/Wed/Fri" and no identifier changed.

Migration `d64100000001`, after `d62200000001`, only widens the cadence and
subscription-type CHECKs. It performs no data or balance mutation. Downgrade
restores the old CHECKs and fails if a row still contains `daily`; there is no
automatic conversion. No dependency or Settings field is added.

### Merged report dispatch

`_REPORT_CADENCES` owns each cadence's node and cron definition, including quote
times. `_REPORT_BATCHES` produces only `report-incremental-weekday` (Mon–Fri,
17:00 ET, cadences Daily and Mon/Wed/Fri) and `report-incremental-weekly`
(Saturday, 19:00 ET, Weekly). Import-time assertions check that each cadence
belongs to exactly one batch and shares its hour/minute.

The batch task accepts `report_type`, `cadences`, `trigger_hour` and
`trigger_minute`; it no longer accepts a caller-supplied node or singular
cadence. Omitted cadences default to Mon/Wed/Fri. Every invocation, including
an argument-less call, applies ET due days derived from the cron definition.
After the unchanged stale-trigger guard, an empty due list returns
`no_due_cadence` and ends telemetry as ok without subscription checks or reports.

All due cadences receive Expired recovery before recipient selection. Recipients
are snapshotted in cadence-table order (Daily before Mon/Wed/Fri), then share one
`batch_now`, one moves cache and a combined `users_remaining` countdown. After
that loop, the full subscription check runs for each due cadence, including
when there are no recipients. Per-user failure isolation, compliance and label
alerts, and all-failed retry behavior remain unchanged. Telemetry records
`cadences` as a list. Tuesday touches only Daily; Wednesday serves both in one
batch. Per-user Ops generation is a separate task and remains unchanged.

Deploy this change together with #651 and #652, outside weekday 16:50–17:40 ET
and Saturday 18:50–19:40 ET. Review, merge, deployment and production data work
require separate owner authorization.

## Validation and rollout

Real-Postgres tests cover the worked examples, atomic insufficient balances,
ledger replay, verified-email requirements, ET daily lock including concurrent
sessions, quotes, signup defaults, migration up/down preserving legacy
cadences, Ops reads/suspension, overview email and unsubscribe behavior.
Five helper examples assert ET date/hour/minute without literal UTC offsets.
The full backend format, lint, type and test gate runs before push.

Migration adds no charges or credits. Downgrade restores the old cadence and
reason CHECKs; rows using `none` or `subscription_return` require explicit
data handling before a rollback. Merge, deployment and production data work
require separate owner authorization. The Ops reference note is not updated
without authorization for that note.


## Scheduled lifecycle (issues #596 and #610)

`active_users` and `active_user_ids` require `subscription_status == active`,
in addition to the existing account, cadence, email and holdings gates.
Cancel-pending active subscriptions still receive reports. On-demand and Ops
report generation are unaffected.

`generate_incremental_report` first calls `run_cadence_checks(session, cadence,
today_et(), statuses=("expired",))` for each due cadence before reading recipients. A resumed user
receives that day's report in the same batch, subject to the existing dispatch
gates. Active renewal, expiry and cancellation still happen only in the full
check after the recipient loop, before the all-failed retry decision, and also
when there are no recipients. The default `statuses` is `("active", "expired")`.
Stale-trigger skips do not check. Check-phase failures are logged and ops-alerted
without changing report results or retry decisions; each failed phase sends its
own alert. If pre-dispatch recovery fails, the full check can resume the user
after the loop, without a report for that user in that batch.

Checks read sorted ids of active accounts on that cadence with active or
expired subscriptions, regardless of holdings or verified email. Each user
is refreshed under `FOR UPDATE`, ignoring the daily user-adjustment lock,
and committed separately; failures roll back that user and continue.

| State / condition | Result |
|---|---|
| Active, today at or before expiry | Unchanged |
| Active, overdue, cancel pending or no verified email | Cancelled, null type, cadence none, clear cancel flag; retain expiry |
| Active, overdue, sufficient balance | Charge one period and renew |
| Active, overdue, insufficient balance | Expired; retain type, cadence, period, expiry and anchor; send expiry notice after commit |
| Expired, verified email and sufficient balance | Charge one period, resume active from today |
| Expired, otherwise | Unchanged |

Normal renewal charges with `subscription:{id}:{old_expiry}:{plan}` and
continues from old expiry using the existing anchor. If
`next_expiry(old_expiry, anchor) < today`, a whole period was missed:
no missed periods are charged, and one fresh period starts today with
today's anchor and charge key, exactly as Expired resume does. Ledger calls
precede column edits. A same-day task retry cannot charge again: a renewed
or resumed user is no longer overdue, and a user moved to Expired keeps the
old expiry date but is now handled by the Expired rules, which charge only
when the balance covers the fee.

Example: expiry 2026-11-17, checked 2026-11-21 -> Weekly charge 0.99,
period 2026-11-17..2026-12-17. Expiry 2026-08-17, checked 2026-11-21 ->
one 0.99 charge keyed 2026-11-21, period 2026-11-21..2026-12-21, anchor 21.
Expired with balance 0.40 stays expired; after a 5.00 grant, a check on
2026-12-05 charges 0.99, leaves 4.41, and resumes through 2027-01-05.
The resumed user receives the 2026-12-05 report in the same batch.

`maybe_send_low_balance_reminder` runs after the commit of a successful
`POST /me/subscription`, scheduled renewal/resume, or non-replayed Paddle
purchase. It sends only for active, non-cancel-pending subscriptions whose
total balance is below the plan fee and whose verified recipient resolves.
There is no notification state table: each charge and each short top-up can
send another reminder. Expiry notice sends only on transition to expired.
Both notices use the user's locale (`en`, `zh`, `zh-Hant`, otherwise English)
and the verified delivery address first, then verified account address.
Provider errors log and return None without rolling back subscription state.
Tests mock notification sends automatically; provider tests mock HTTP.

Launch activation uses `activate_existing` and the shared fresh-subscribe
helper, without setting `subscription_adjusted_on` or sending notices.
Already-processed subscriptions are skipped. Inactive active accounts with
verified email and weekly/mwf cadence are charged from launch day. Other
inactive accounts keep inactive status and get cadence none. Unexpected
insufficient balance is reported as insufficient and also sets cadence none.
The script defaults to a read-only dry run and commits one user at a time
under `--apply`; re-running cannot charge again. See
[Deployment](../deployment.md#subscription-launch-activation-issue-596)
for sequencing and authorization.


## First-subscription referral reward (issue #675)

`_fresh_subscribe` calls `referral_subscription_bonus` immediately after
`consume_credits` and before editing subscription state. The caller commits
the charge, reward and state together. This helper also serves plan changes,
expired resumes and late renewals, so eligibility is decided from the ledger:
exactly one distinct subscription charge key for the referee, including the
newly posted charge. A gift/cash split counts once. The first reward is the
signup grant setting times the configured rate, rounded half up to cents;
a zero result consumes first-charge eligibility. Later charges never qualify.
The email-hash reward key prevents another issued reward after purge and
re-registration. Missing referrer accounts, including root Admin, receive
nothing. There are no reward notifications or grand-referrer rewards.
See [Credit ledger](credit-ledger.md#referral-rewards-and-refund-clawbacks-issue-675).


## Jade tier (issue #710)

Jade costs 9.99 credits per calendar-month period and includes Advanced access.
`is_jade(user)` means active status and type `jade`, including cancel-pending
and overdue subscriptions awaiting lifecycle processing. Backend access uses
only `is_advanced` and `is_jade`; the session probe exposes both flags without
another user query. Existing agent and snapshot gates therefore admit Jade.
No risk tool ships in this issue.
Jade Pass 2 and analyze regenerate use `JADE_PASS2_MODEL` (`anthropic/claude-haiku-5.5`) with high reasoning, `data_collection=deny` and no provider pin, selected by the current `is_jade(user)` state (issue #725).

`BRIEFING_PLANS = ("weekly", "mwf", "daily")` names both briefing types and
Jade's available cadences. Every fresh charge and quote uses
`resulting_cadence`: briefing types map to themselves; Jade inherits a valid
cadence from an active subscription, or retains it when its existing type is
Jade (including Expired recovery). All other Jade subscriptions start Daily.
The helper runs before subscription state changes. Renewal and late recovery
preserve Jade's chosen cadence; checks and dispatch remain cadence-based.

`PATCH /me/jade/cadence` accepts `{"cadence":"weekly"}`, `mwf` or `daily`;
invalid values return 422. It locks and refreshes the row, requires `is_jade`
(409 `not_jade` with rollback otherwise), flushes and commits the cadence, and
returns the subscription summary. Repeating the current cadence succeeds.
There is no ledger write, adjustment-date write, reminder or daily lock.

`POST /me/subscription` admits `jade`. Active Jade switching to a briefing
plan returns 409 `jade_managed`, after the existing daily-limit check. A same-day
request therefore returns `daily_limit` first. Same-plan, non-pending active
subscriptions still return `no_change`, even when overdue. Jade cancellation
and resume reuse the existing endpoints and billing rules. To move to a
briefing plan, cancel Jade, wait for lifecycle cancellation after its paid
period, then subscribe in Profile; Expired Jade may also choose a briefing plan.
Quotes and summaries include cadence; quote times and holdings warnings use
that cadence, rather than type.

Example: Weekly paid 0.99 gift credits for 2026-10-01 through 2026-11-01;
change on 2026-10-16 with balance 20.00 returns 0.53 gift credits (17/32 days),
then charges 9.99 under `subscription:{id}:2026-10-16:jade`. Jade keeps Weekly,
starts 2026-10-16 through 2026-11-16 with anchor 16, and leaves 10.54. An inactive
user starting with 10.00 gets Daily, balance 0.01 and a low-balance reminder.
Changing cadence repeatedly that day succeeds without another charge.

Migration `d71000000001`, after `d68100000001`, only widens the subscription-type
CHECK. Downgrade restores the previous CHECK and fails while Jade rows exist;
there is no conversion or data operation. Notice plan labels are Jade in English
and hand-authored Chinese in each locale; low-balance and expiry notices still
link to Profile for top-up. No dependency, Settings or Beat change is required.
Real-Postgres tests cover D10 and A1-A7. Merge, review, deployment and production
migration each require separate owner authorization.
