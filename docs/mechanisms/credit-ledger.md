# Credit ledger write path (issue #562, PR-A)

Credit balances use two `NUMERIC(12,2)` columns on `users`: `credit_cash_balance` has cash value; `credit_gift_balance` has no cash value. Both default to zero and have nonnegative CHECK constraints. Internally, one credit equals one USD; product copy says only “credits”.

`credit_ledger` records every balance change. Its `BIGINT GENERATED ALWAYS AS IDENTITY` primary key orders rows for reconstruction. Each row has a plain `user_id` UUID with no foreign key, one `bucket` (`cash` or `gift`), signed `amount`, that bucket's `balance_after`, `reason`, `actor_type` (`system` or `admin`), reserved nullable `actor_id`, `idempotency_key`, optional `note` and `reference`, `created_at`, and nullable `user_deleted_at`. `actor_id` remains NULL in this issue. `(idempotency_key, bucket)` is unique, allowing one consumption to have gift and cash rows with the same key. The `(user_id, id)` index supports ordered reconstruction. The migration adds structure only; it does not grant credits to existing users.

## Invariants and writer

For each live user and bucket, the stored balance equals both the sum of that user's ledger amounts and the highest-id row's `balance_after` (or zero if no rows exist). Balances and `balance_after` cannot be negative. `app/services/credit_ledger.py` is the only writer of balance columns and new ledger rows. Its public write functions lock the `users` row with `SELECT FOR UPDATE`, flush, and leave commit to their caller. Balance updates and rows therefore commit together. Ledger rows are never deleted or rewritten; purge may only set `user_deleted_at`.

`_REASON_RULES` assigns bucket and sign: `recharge` and `invite_rebate` credit cash; `signup_grant` credits gift; `admin_adjustment` credits or debits gift; `subscription` and `qa` debit gift and/or cash. To add a reason, update `REASONS` and `_REASON_RULES` in the service and add a migration that rewrites the `reason` CHECK. Migrations must freeze the reason list rather than importing the live Python tuple.

`consume_credits` spends gift first, then cash. It rejects the whole request if the combined balance is short. This PR implements and tests it without adding a product caller.

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
