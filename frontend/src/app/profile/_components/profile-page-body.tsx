"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { Me } from "@/lib/api";
import { ChangePasswordForm } from "./change-password-form";
import { PendingVerificationsList } from "./pending-verifications-list";

// Split out from page.tsx (issue #220), same reasoning as
// questionnaire-page-body.tsx: whether the page renders at all depends on
// translated content (the load-error message), so it isn't worth carving
// that condition back into the Server Component just to keep this file
// heading-only.
export function ProfilePageBody({ me, hadLoadError }: { me: Me | null; hadLoadError: boolean }) {
  const t = useTranslations("profile");

  if (hadLoadError || !me) {
    return (
      <p className="text-sm text-destructive" role="alert">
        {t("errorLoadFailed")}
      </p>
    );
  }

  // Design decision (this PR): delivery_email unset falls back to the
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

  return (
    <div className="flex flex-col gap-6">
      <header>
        <h1 className="font-heading text-2xl font-semibold">{t("pageTitle")}</h1>
      </header>

      {me.missing.length > 0 && (
        <Card>
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
          <CardTitle>{t("passwordHeading")}</CardTitle>
        </CardHeader>
        <CardContent className="px-4">
          <ChangePasswordForm />
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

      <Card>
        <CardHeader>
          <CardTitle>{t("deliveryEmailHeading")}</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-1.5 px-4">
          <span className="text-sm">{deliveryEmailDisplay}</span>
          {!me.delivery_email && (
            <span className="text-xs text-muted-foreground">{t("deliveryEmailFallbackNote")}</span>
          )}
        </CardContent>
      </Card>

      {/* Issue #262 §8.2/§8.4: actionable email verifications. Renders
          nothing when the list is empty (verified/expired history is
          deliberately not surfaced). Sits directly under the delivery-email
          block — same topic, per Profile Page.md §8.4. */}
      {me.pending_email_verifications.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>{t("emailVerificationHeading")}</CardTitle>
            <CardDescription>{t("emailVerificationBody")}</CardDescription>
          </CardHeader>
          <CardContent className="px-4">
            <PendingVerificationsList verifications={me.pending_email_verifications} />
          </CardContent>
        </Card>
      )}

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

      <Card>
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
