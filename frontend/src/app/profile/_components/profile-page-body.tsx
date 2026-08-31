"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { Me } from "@/lib/api";
import { ChangePasswordForm } from "./change-password-form";
import { PendingVerificationsList } from "./pending-verifications-list";
import { useVerificationResend } from "./use-verification-resend";

// Split out from page.tsx (issue #220), same reasoning as
// questionnaire-page-body.tsx: whether the page renders at all depends on
// translated content (the load-error message), so it isn't worth carving
// that condition back into the Server Component just to keep this file
// heading-only.
export function ProfilePageBody({ me, hadLoadError }: { me: Me | null; hadLoadError: boolean }) {
  const t = useTranslations("profile");
  // Shared with PendingVerificationsList (issue #269 §6: the delivery-email
  // section's inline resend runs the same flow).
  const resend = useVerificationResend();

  if (hadLoadError || !me) {
    return (
      <p className="text-sm text-destructive" role="alert">
        {t("errorLoadFailed")}
      </p>
    );
  }

  // Design decision (issue #220 PR): delivery_email unset falls back to the
  // account email for display, with a note — mirrors the backend's own
  // recipient_email() fail-open-to-account-email semantics, but made
  // visible to the user instead of silent.
  const deliveryEmailDisplay = me.delivery_email ?? me.email;

  // Gap card (issue #221 §2.6): guidance only, never forced — renders
  // nothing when `missing` is empty. Buttons never carry ?onboarding=1
  // (that query string has exactly one trigger, the post-signup redirect).
  const missingLinks: Record<"questionnaire" | "holdings", { href: string; label: string }> = {
    questionnaire: { href: "/questionnaire", label: t("missingSetupQuestionnaire") },
    holdings: { href: "/holdings", label: t("missingSetupHoldings") },
  };

  // Issue #269 §1/§3: the Email Verification section renders when there are
  // actionable records (pending/undeliverable), OR when the account has no
  // verified receiving address at all (both timestamps null — the "reports
  // will not be sent" state). Nothing pending AND at least one verified
  // address → hidden, same as before #269.
  const noVerifiedRecipient = me.email_verified_at == null && me.delivery_email_verified_at == null;
  const showEmailVerification = me.pending_email_verifications.length > 0 || noVerifiedRecipient;

  // Issue #269 §6: unverified is derived per scope — a set delivery_email is
  // checked against its own timestamp; the account-email fallback against
  // the account timestamp.
  const deliveryEmailUnverified =
    (me.delivery_email != null && me.delivery_email_verified_at == null) ||
    (me.delivery_email == null && me.email_verified_at == null);
  // The inline Resend button needs an existing resendable record for the
  // displayed address — resend only accepts the caller's own
  // pending/undeliverable rows. No record → the note still shows, no button.
  const deliveryResendTarget = me.pending_email_verifications.find(
    (v) =>
      v.email === deliveryEmailDisplay &&
      v.purpose === (me.delivery_email != null ? "delivery_email" : "account_email"),
  );

  return (
    <div className="flex flex-col gap-6">
      <header>
        <h1 className="font-heading text-2xl font-semibold">{t("pageTitle")}</h1>
      </header>

      {me.missing.length > 0 && (
        <Card variant="urgent">
          <CardHeader>
            <CardTitle>{t("missingSetupHeading")}</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-wrap gap-2 px-4">
            {me.missing.map((item) => {
              const link = missingLinks[item as "questionnaire" | "holdings"];
              if (!link) return null;
              return (
                <Button key={item} variant="outline" render={<Link href={link.href} />}>
                  {link.label}
                </Button>
              );
            })}
          </CardContent>
        </Card>
      )}

      {/* Issue #269 §1/§2: second section on the page, right after the gap
          card slot (whether or not that slot renders). Urgency styling per
          §2 — incomplete-setup nudge, same language as the gap card. */}
      {showEmailVerification && (
        <Card variant="urgent">
          <CardHeader>
            <CardTitle>{t("emailVerificationHeading")}</CardTitle>
            <CardDescription>{t("emailVerificationBody")}</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-3 px-4">
            {noVerifiedRecipient && (
              <p className="text-sm">{t("emailVerificationNoRecipient")}</p>
            )}
            <PendingVerificationsList verifications={me.pending_email_verifications} />
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle>{t("accountHeading")}</CardTitle>
        </CardHeader>
        <CardContent className="px-4">
          <div className="flex flex-col gap-1.5">
            <span className="text-sm text-foreground/80">{t("accountEmailLabel")}</span>
            <span className="text-sm">{me.email}</span>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t("investmentStyleHeading")}</CardTitle>
          <CardDescription>{t("investmentStyleBody")}</CardDescription>
        </CardHeader>
        <CardContent className="px-4">
          <Button variant="outline" render={<Link href="/questionnaire" />}>
            {t("investmentStyleButton")}
          </Button>
        </CardContent>
      </Card>

      {/* Issue #269 §6: an unverified shown address renders gray italic with
          a note and, when a resendable record exists for it, an inline
          Resend button. Known overlap with the top Email Verification
          section's list is intentional (global cue vs. in-place action). */}
      <Card>
        <CardHeader>
          <CardTitle>{t("deliveryEmailHeading")}</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-1.5 px-4">
          <div className="flex items-center justify-between gap-3">
            <span
              className={
                deliveryEmailUnverified
                  ? "text-sm italic text-muted-foreground"
                  : "text-sm"
              }
            >
              {deliveryEmailDisplay}
            </span>
            {deliveryEmailUnverified && deliveryResendTarget && (
              <Button
                variant="outline"
                disabled={resend.pendingId !== null}
                onClick={() => void resend.handleResend(deliveryResendTarget.id)}
              >
                {resend.pendingId === deliveryResendTarget.id
                  ? t("emailVerificationResending")
                  : t("emailVerificationResendButton")}
              </Button>
            )}
          </div>
          {deliveryEmailUnverified && (
            <span className="text-xs text-muted-foreground">{t("deliveryEmailUnverifiedNote")}</span>
          )}
          {!me.delivery_email && (
            <span className="text-xs text-muted-foreground">{t("deliveryEmailFallbackNote")}</span>
          )}
          {resend.error && (
            <p className="text-sm text-destructive" role="alert">
              {resend.error}
            </p>
          )}
        </CardContent>
      </Card>

      {/* Placeholders below (issue #220 §2 requirements 4/5/7/8): visible and
          labeled not-yet-implemented, never a form a click could actually
          submit — every control here is disabled. */}

      <Card>
        <CardHeader>
          <CardTitle>{t("portfolioOverviewHeading")}</CardTitle>
        </CardHeader>
        <CardContent className="px-4">
          <p className="text-sm text-muted-foreground">{t("portfolioOverviewPlaceholder")}</p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t("reportScheduleHeading")}</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-2 px-4">
          <select
            disabled
            aria-label={t("reportScheduleHeading")}
            className="w-fit rounded-md border border-white/10 bg-transparent px-2 py-1.5 text-sm text-foreground/60"
            defaultValue="weekly"
          >
            <option value="weekly">{t("reportScheduleOptions.weekly")}</option>
            <option value="everyOtherDay">{t("reportScheduleOptions.everyOtherDay")}</option>
            <option value="morning">{t("reportScheduleOptions.morning")}</option>
            <option value="evening">{t("reportScheduleOptions.evening")}</option>
            <option value="morningAndEvening">
              {t("reportScheduleOptions.morningAndEvening")}
            </option>
          </select>
          <p className="text-sm text-muted-foreground">{t("reportSchedulePlaceholder")}</p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t("inviteHeading")}</CardTitle>
          <CardDescription>{t("inviteBody")}</CardDescription>
        </CardHeader>
        <CardContent className="px-4">
          <p className="text-sm text-muted-foreground">{t("invitePlaceholder")}</p>
        </CardContent>
      </Card>

      {/* Issue #269 §4: Change password moved here, just before Delete
          account — it is no longer the second section after Account. */}
      <Card>
        <CardHeader>
          <CardTitle>{t("passwordHeading")}</CardTitle>
        </CardHeader>
        <CardContent className="px-4">
          <ChangePasswordForm />
        </CardContent>
      </Card>

      {/* Issue #269 §5: GitHub-style danger zone — thin red border only, no
          fill. Distinct from the pink-fill urgency treatment above:
          "destructive, be careful" vs "complete this soon". Section itself
          unchanged (button still disabled, still a placeholder per #220
          requirement 8). */}
      <Card variant="danger">
        <CardHeader>
          <CardTitle>{t("deleteAccountHeading")}</CardTitle>
          <CardDescription>{t("deleteAccountBody")}</CardDescription>
        </CardHeader>
        <CardContent className="px-4">
          <Button variant="destructive" disabled>
            {t("deleteAccountHeading")}
          </Button>
          <p className="mt-2 text-sm text-muted-foreground">{t("deleteAccountPlaceholder")}</p>
        </CardContent>
      </Card>
    </div>
  );
}
