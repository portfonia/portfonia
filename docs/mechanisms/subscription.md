# Subscription core (issues #595 and #600)

This backend core implements user subscription operations. Scheduled renewal,
expiry, auto-resume, notification emails and existing-user activation belong
to #596; frontend and public copy belong to #597; cash purge/adjustments belong
to #599. These issues are deployed together. No production operation is part
of this implementation.

## State and ownership

`app/services/subscription.py` owns subscription state and `report_cadence`,
apart from signup's initial `none`. `app/services/credit_ledger.py` remains the
only balance and ledger writer. The migration `s59500000001` adds:

| User column | Type / default | Meaning |
|---|---|---|
| `subscription_status` | Non-null text, `inactive` | `active`, `expired`, `cancelled`, `inactive` |
| `subscription_type` | Nullable text | `weekly` or `mwf` |
| `subscription_expires_on` | Nullable date | Last valid ET day of the paid period |
| `subscription_period_start` | Nullable date | First paid day and charge-key date |
| `subscription_anchor_day` | Nullable smallint, 1..31 | Calendar-month anchor |
| `subscription_cancel_pending` | Non-null boolean, false | Cancel requested during a paid period |
| `subscription_adjusted_on` | Nullable date | Last user adjustment's ET date |

New signups are inactive with cadence `none`. Migration preserves every
existing cadence and gives existing rows inactive subscription defaults.
The post-activation invariant is cadence equals type when present, otherwise
`none`; type is null for inactive/cancelled states. Activation is deferred to
#596, so that invariant does not yet hold for legacy rows. Scheduled fan-out
queries are unchanged here.

## Billing and transactions

`PLAN_FEES` is Weekly 0.99 credits/month and Mon/Wed/Fri 1.99 credits/month.
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
an email does not automatically resume a cancelled subscription. Transition
to cancelled after expiry is deferred to #596.

## API and integrations

All subscription endpoints use `current_principal`:

| Endpoint | Result |
|---|---|
| `GET /me` | Adds `subscription`: status, nullable type/expiry, cancel flag, nullable next adjustment datetime |
| `GET /me/subscription/quote?type=weekly\|mwf` | Read-only quote; no user lock or writes |
| `POST /me/subscription` with `{"type":"weekly"}` or `{"type":"mwf"}` | Subscribe, change or same-plan resume; returns subscription summary |
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
`needs_holdings`, and nullable `blocked` (`daily_limit` or `email_unverified`).
Numbers remain present when blocked. `needs_holdings` warns for Mon/Wed/Fri
with zero holdings, without refusing the subscription.

Ops directory rows gain status, type, expiry and cancellation flag; its cadence
filter accepts `none`. The existing Ops cadence route returns 409
`cadence changes are suspended; cadence follows the subscription` without
reading/writing the user. Portfolio overview emails omit the next-report line
when cadence is `none`.

`next_occurrence_for_cadence` reads fire times only from `_REPORT_CADENCES`.
Issue #600 corrects displayed times by adding Celery's absolute remaining
duration in UTC, then converting back to ET. No offset, timezone-transition
rule or date is hard-coded; Beat schedules and cadence definitions are unchanged.
For the quote at 2026-10-31 noon ET, the next Mon/Wed/Fri report is
2026-11-02 at 17:00 ET.

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
