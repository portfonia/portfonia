# Credit ledger write path (issue #562, PR-A)

Credit balances use two `NUMERIC(12,2)` columns on `users`: `credit_cash_balance` has cash value; `credit_gift_balance` has no cash value. Both default to zero. Gift retains its nonnegative CHECK; cash may be negative after a referral clawback (#675). Internally, one credit equals one USD; product copy says only “credits”.

`credit_ledger` records every balance change. Its `BIGINT GENERATED ALWAYS AS IDENTITY` primary key orders rows for reconstruction. Each row has a plain `user_id` UUID with no foreign key, one `bucket` (`cash` or `gift`), signed `amount`, that bucket's `balance_after`, `reason`, `actor_type` (`system`, `admin`, or `user`), reserved nullable `actor_id`, `idempotency_key`, optional `note` and `reference`, `created_at`, and nullable `user_deleted_at`. `actor_id` remains NULL in this issue. `(idempotency_key, bucket)` is unique, allowing one consumption to have gift and cash rows with the same key. The `(user_id, id)` index supports ordered reconstruction. The migration adds structure only; it does not grant credits to existing users.

## Invariants and writer

For each live user and bucket, the stored balance equals both the sum of that user's ledger amounts and the highest-id row's `balance_after` (or zero if no rows exist). Gift balances and gift `balance_after` cannot be negative. A debit cannot leave a negative balance except a cash `referral_clawback` with `allow_negative=True`. Positive amounts may improve a negative cash balance without fully settling it. `app/services/credit_ledger.py` is the only writer of balance columns and new ledger rows. Its public write functions lock the `users` row with `SELECT FOR UPDATE`, refresh the loaded user from the database, flush, and leave commit to their caller. Callers must flush any pending edits to that `User` before calling a credit write function, or the lock query will overwrite those in-memory edits. Balance updates and rows therefore commit together. Ledger rows are never deleted or rewritten; purge may only set `user_deleted_at`.

`_REASON_RULES` assigns bucket and sign: `recharge` and `invite_rebate` credit cash; `signup_grant` credits gift; `admin_adjustment` credits or debits cash or gift; `refund` debits purchased cash credits or reverses a rejected refund; `subscription` and `qa` debit gift and/or cash; `subscription_return` credits cash and/or gift. To add a reason, update `REASONS` in `app/models/credit_ledger.py` and `_REASON_RULES` in `app/services/credit_ledger.py`, then add a migration that rewrites the `reason` CHECK. Migrations must freeze the reason list rather than importing the live Python tuple.

Paddle purchase rows use `recharge:paddle:{transaction_id}`. Ops refund debits
use `refund:{transaction_id}:{request_key}` and store the Paddle adjustment id
as their reference. Rejected-adjustment reversals use
`refund_reversal:{transaction_id}:{adjustment_id}`. These keys make webhook
redelivery idempotent, and make an Ops refund retry idempotent once its debit
row is committed. One exception: if Paddle creates the adjustment but the
local commit then fails, the debit is rolled back and the endpoint sends a
"Paddle refund ledger commit failed" alert with the adjustment id. Do not
retry that request; reconcile the adjustment in the Paddle dashboard first,
because a retry with the same key finds no row and would refund again.

`consume_credits` spends gift first, then cash. It rejects the whole request if the combined balance is short. Subscription charges now call it with reason `subscription` and key `subscription:{user_id}:{period_start}:{plan}` (issue #595).

`return_subscription_credits` posts one system credit per non-zero bucket,
with reason `subscription_return` and key `subscription_return:{old_charge_key}`.
The subscription service computes the pro-rata return from the original
charge rows, returns cash first up to that charge's cash portion, and returns
the remainder as gift. A matching user/reason/summed-amount replay returns the
original rows; a conflict raises `IdempotencyConflict`, and a zero total writes
nothing. Ledger writes precede subscription state edits in the same transaction.
See [Subscription core](subscription.md) for the billing and cancellation rules.

## Idempotency and deletion

Signup uses `signup_grant:<sha256(normalized email)>`, where normalization is the existing invite normalizer (strip and lowercase). A prior row with this key blocks a new grant, including after purge and re-registration. The default grant is `SIGNUP_GRANT_CREDITS=5.00`; zero disables it. Admin adjustments prepend `admin_adjustment:` to the caller's key. Consumption callers supply their own namespaced key (for example, `qa:<id>`); a split consumption shares that key across both rows. Matching replays return the original entries without another balance change; conflicting user, reason, or amount raises an idempotency conflict. Admin adjustment replays also require the same bucket; reusing a key for another bucket returns 409 even though ledger uniqueness is per bucket.

Hard purge marks this user's unmarked rows with `user_deleted_at`, reports the count as `deleted.credit_ledger_flagged`, and deletes the user row. The ledger's `user_id` remains intact for historical reconstruction. The Ops endpoint `POST /admin/users/by-email/credit-adjustments` accepts `bucket` (`gift` or `cash`, default `gift`) since issue #599. Omitting it preserves the existing gift adjustment behavior. It requires a signed nonzero two-decimal amount, a non-blank note, and an idempotency key. For cash adjustments, the note records where the value went, such as an explicit user waiver after the refund window. An overdraft returns 409 `insufficient balance`; a key conflict returns 409 `idempotency_key already used for a different adjustment`. The response shape is unchanged, with `entry.bucket` showing the selected bucket. Cash adjustments do not change a purchase's refundable remainder, which is computed from purchase and refund rows only. A later refund exceeding the available cash balance is still refused with 409 `insufficient cash balance`.

Both Ops purge routes refuse a local user with `credit_cash_balance != 0` (issue #675), after the confirm checks and before Auth deletion, with 409 `user has a non-zero cash balance; settle it to zero first`. The refusal leaves Auth and local rows untouched. Zero cash with a positive gift balance remains purgeable; Auth-only orphans are unchanged. There is no automatic balance zeroing in Ops purge. Self-service deletion (#644) records an explicit voluntary relinquishment before shared purge.

## Existing-user backfill

After deployment, an operator can inspect the default dry run:

```sh
cd backend
python -m app.scripts.backfill_signup_grants
```

The script scans every current `users` row, regardless of status. It reports `would-grant` or `already-granted` by signup hash without writing. `--apply` commits one user at a time using the same grant service and a backfill note. Re-running it grants nothing twice. Production dry run and `--apply` are separate owner-authorized data operations; opening this PR does not run either.

## Read path (PR-B)

`GET /admin/credits/ledger.csv` exports all ledger rows by ascending `id`, with the columns `id,created_at,user_id,email,bucket,amount,balance_after,reason,actor_type,actor_id,idempotency_key,reference,note,user_deleted_at`. Email comes from a left join to `users`, so a purged user's email is empty while `user_deleted_at` remains present. `GET /admin/credits/balances.csv` exports one row per current user by email, with `user_id,email,status,cash_balance,gift_balance,total_balance,ledger_cash_sum,ledger_gift_sum,consistent`. The sums are ledger amounts per bucket, and `consistent` is true only when both stored balances match their respective sums. Both endpoints use the existing Ops token and audit path, require no parameters, return in-memory CSV with fixed two-decimal amounts, and attach a filename dated with `today_et()`. Timestamps use ET ISO 8601 with an offset; NULL timestamps are empty.

`GET /me` includes `credit_balance`, the sum of cash and gift balances, serialized as a decimal string. The Profile Account card displays that string verbatim under the `Credits` label in all three UI locales, without currency formatting.


## Voluntary relinquishment at account deletion (issue #644)

`relinquish_cash(session, user, amount, refundable_part)` requires the caller
to hold the refreshed user row lock. It posts one negative cash entry for
the full current balance, reason `relinquish`, actor type `user`, key
`relinquish:{user.id}`, and note `Relinquished by user at account deletion;
refundable within 120 days at that time: {refundable_part}`. The deletion
route rechecks the exact submitted balance and email and verifies a
purpose-distinct Altcha proof first. Purge flags the new row with
`user_deleted_at` in the same transaction; no endpoint records relinquishment
without account deletion.

`refundable_cash(session, user_id, now)` sums the unrefunded remainders of
cash `recharge:paddle:*` rows with `created_at >= now - 120 days`, including
refund reversal adjustments through `purchase_refundable`, and caps that
sum at the current cash balance, floored at zero. This is a disclosure,
not a deletion refusal; the user may give up the refundable portion too.

Migration `d64400000001` widens `ck_credit_ledger_reason` and
`ck_credit_ledger_actor_type` using `op.f(...)`. Downgrade restores the old
lists and fails if rows with the new values exist. Retained history must
not be removed to force downgrade. Deployment needs separate authorization.


## Referral rewards and refund clawbacks (issue #675)

`referral_bonus` credits gift or cash; `referral_clawback` debits cash or
credits cash on reversal. The writer posts each reward and clawback in the
same transaction as the subscription charge, purchase, refund or rejected
refund that triggers it. Gift-first consumption and combined-balance
sufficiency are unchanged. Profile displays the combined balance verbatim,
including a negative value.

The first-subscription reward is `SIGNUP_GRANT_CREDITS *
REFERRAL_FIRST_SUBSCRIPTION_RATE` (default 0.20), rounded to cents with
`ROUND_HALF_UP`, credited as gift. Eligibility requires exactly one distinct
subscription charge key for that referee, including the charge just posted;
a split gift/cash charge counts once. A zero first reward still consumes
eligibility. The email-hash key `referral_subscription:{sha256(normalized
referee email)}` also survives referee purge and prevents an issued reward
from being paid again after re-registration. Later plan changes, resumes and
late renewals do not qualify.

Each Paddle purchase earns `credits * REFERRAL_RECHARGE_RATE` (default 0.15),
rounded half up, as cash with key `referral_recharge:{transaction_id}`.
Rates are constrained to 0..1. Missing referrer user rows and zero amounts
write nothing. The root Admin has no account and never gets a ledger row;
services use user-row existence rather than ambient root identity.

For refunds, use the recorded purchase and bonus amounts, not today's rate:

```text
net_refunded = purchase_credits - purchase_refundable_remainder
target = round_half_up_cent(bonus * net_refunded / purchase_credits)
already = -sum(referral_clawback amounts for this transaction, including reversals)
new_clawback = -(target - already), only when target > already
```

A 1.50 reward on a 10-credit purchase, refunded as 3 then 7 credits, produces
-0.45 then -1.05. Cash may end at -1.50 after the reward has been spent.
Keys are `referral_clawback:{tx}:{request_key}` and
`referral_clawback_reversal:{tx}:{adjustment_id}`; both reference the original
`refund:{tx}:{request_key}`. A rejected refund restores that refund's recorded
clawback once, even if cash remains negative. Replays write nothing.

If the bonus recipient has been purged, skip clawback and commit the refund.
After commit, the existing admin alert task enqueues an INFO notice with the
referee email, transaction, refunded credits and unrecovered reward, using
`referral-clawback-skipped:{tx}:{request_key}`. Enqueue failure is logged only;
a later rejection writes no referral reversal for a deleted referrer. No
bonus means no clawback or skipped-clawback notice.

Migration `d67500000001` removes `ck_users_credit_cash_balance`, allows negative
cash `credit_ledger.balance_after`, and adds the two reasons. Gift CHECKs
remain. Downgrade fails while negative cash values or retained rows with the
new reasons exist; retained history must not be erased to force rollback.
