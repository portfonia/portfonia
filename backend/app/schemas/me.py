from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class PendingVerificationOut(BaseModel):
    """One actionable email-verification row for the Profile page's list
    (issue #262, Ring 1-Profile Page.md §8.2). `status` is "pending" or
    "undeliverable" — undeliverable is included because a typo'd address
    that bounced leaves the user with nothing visibly "pending" (2026-08-30
    production-testing gap). No token_hash / provider_message_id here: the
    token is a credential (hash-only storage discipline), and the message
    id is Ops-diagnostic surface, not user surface."""

    id: str
    purpose: str
    email: str
    status: str
    expires_at: datetime
    last_sent_at: datetime


class MeOut(BaseModel):
    """GET /me response — issue #220, full #221 shape landed in one PR (see
    Ring 1-Onboarding.md §6: don't build a narrow schema now and widen it
    later). `missing` only ever contains "questionnaire"/"holdings" — never
    "tos"; `tos_accepted_at` is audit-only (§2.6).

    Issue #269: the raw verification timestamps (mirroring the `users`
    columns) ride along so the Profile page can derive verification state
    without a new endpoint — both NULL means "no verified receiving
    address" (the frontend's no-valid-recipient condition)."""

    email: str
    delivery_email: str | None
    email_verified_at: datetime | None
    delivery_email_verified_at: datetime | None
    tos_accepted_at: datetime | None
    has_questionnaire: bool
    has_holdings: bool
    missing: list[str]
    # issue #262 §8.2: the calling user's own actionable verifications.
    # ops_manual rows are always user_id=NULL (§3.5) so they can never
    # appear here; the router filters by the principal's own user_id.
    pending_email_verifications: list[PendingVerificationOut]
    # Issue #308: sourced from users.locale, deliberately named differently
    # from the frontend's own Locale/LOCALES UI-chrome type (issue #209) —
    # "report language" and "UI locale" are different concepts that happen
    # to share one DB column today; naming them apart at the API boundary
    # keeps that from becoming another overlapping-term drift (the Custodian
    # label precedent, glossary-consistency.test.ts).
    report_language: str
