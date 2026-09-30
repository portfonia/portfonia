# Credit ledger write path (issue #562, PR-A)

Credit balances use two `NUMERIC(12,2)` columns on `users`: `credit_cash_balance` has cash value; `credit_gift_balance` has no cash value. Both default to zero and have nonnegative CHECK constraints. Internally, one credit equals one USD; product copy says only “credits”.

`credit_ledger` records every balance change. Its `BIGINT GENERATED ALWAYS AS IDENTITY` primary key orders rows for reconstruction. Each row has a plain `user_id` UUID with no foreign key, one `bucket` (`cash` or `gift`), signed `amount`, that bucket's `balance_after`, `reason`, `actor_type` (`system` or `admin`), reserved nullable `actor_id`, `idempotency_key`, optional `note` and `reference`, `created_at`, and nullable `user_deleted_at`. `actor_id` remains NULL in this issue. `(idempotency_key, bucket)` is unique, allowing one consumption to have gift and cash rows with the same key. The `(user_id, id)` index supports ordered reconstruction. The migration adds structure only; it does not grant credits to existing users.

## Invariants and writer

For each live user and bucket, the stored balance equals both the sum of that user's ledger amounts and the highest-id row's `balance_after` (or zero if no rows exist). Balances and `balance_after` cannot be negative. `app/services/credit_ledger.py` is the only writer of balance columns and new ledger rows. Its public write functions lock the `users` row with `SELECT FOR UPDATE`, refresh the loaded user from the database, flush, and leave commit to their caller. Callers must flush any pending edits to that `User` before calling a credit write function, or the lock query will overwrite those in-memory edits. Balance updates and rows therefore commit together. Ledger rows are never deleted or rewritten; purge may only set `user_deleted_at`.

`_REASON_RULES` assigns bucket and sign: `recharge` and `invite_rebate` credit cash; `signup_grant` credits gift; `admin_adjustment` credits or debits gift; `refund` debits purchased cash credits or reverses a rejected refund; `subscription` and `qa` debit gift and/or cash; `subscription_return` credits cash and/or gift. To add a reason, update `REASONS` in `app/models/credit_ledger.py` and `_REASON_RULES` in `app/services/credit_ledger.py`, then add a migration that rewrites the `reason` CHECK. Migrations must freeze the reason list rather than importing the live Python tuple.

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

Signup uses `signup_grant:<sha256(normalized email)>`, where normalization is the existing invite normalizer (strip and lowercase). A prior row with this key blocks a new grant, including after purge and re-registration. The default grant is `SIGNUP_GRANT_CREDITS=5.00`; zero disables it. Admin adjustments prepend `admin_adjustment:` to the caller's key. Consumption callers supply their own namespaced key (for example, `qa:<id>`); a split consumption shares that key across both rows. Matching replays return the original entries without another balance change; conflicting user, reason, or amount raises an idempotency conflict.

Hard purge marks this user's unmarked rows with `user_deleted_at`, reports the count as `deleted.credit_ledger_flagged`, and deletes the user row. The ledger's `user_id` remains intact for historical reconstruction. The new Ops endpoint `POST /admin/users/by-email/credit-adjustments` changes only gift credit; it requires a signed nonzero two-decimal amount, a note, and an idempotency key. An overdraft or key conflict returns 409.

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
