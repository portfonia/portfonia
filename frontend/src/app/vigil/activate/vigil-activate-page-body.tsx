"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  armVigilVault,
  getVigilVaultStatus,
  listVigilConfirmationEmails,
  pauseVigilVault,
  sendVigilConfirmationEmailVerification,
  VigilApiError,
  VigilRevisionConflictError,
  type VigilConfirmationEmail,
  type VigilVaultStatus,
} from "@/lib/vigil/api";

const ACTIVE_PHASES = new Set(["ARMED", "CHALLENGE_1", "CHALLENGE_2", "FINAL_WARNING"]);

export function VigilActivatePageBody() {
  const t = useTranslations("vigil.activate");
  const router = useRouter();
  const [vault, setVault] = useState<VigilVaultStatus | null>(null);
  const [emails, setEmails] = useState<VigilConfirmationEmail[]>([]);
  const [error, setError] = useState(false);
  const [busy, setBusy] = useState(false);

  const handleError = useCallback(
    (reason: unknown) => {
      if (reason instanceof VigilApiError && reason.status === 401) {
        router.push("/login?reason=expired&next=/vigil/activate");
        return;
      }
      setError(true);
    },
    [router],
  );

  const refresh = useCallback(async () => {
    try {
      const [nextVault, nextEmails] = await Promise.all([
        getVigilVaultStatus(),
        listVigilConfirmationEmails(),
      ]);
      setVault(nextVault);
      setEmails(nextEmails);
      setError(false);
    } catch (reason) {
      handleError(reason);
    }
  }, [handleError]);

  useEffect(() => {
    const timer = window.setTimeout(() => void refresh(), 0);
    return () => window.clearTimeout(timer);
  }, [refresh]);

  useEffect(() => {
    if (!vault?.pending || vault.pending.confirmation_email_verified) return;
    const timer = window.setInterval(() => void refresh(), 5000);
    return () => window.clearInterval(timer);
  }, [refresh, vault?.pending]);

  const run = useCallback(
    async (action: () => Promise<unknown>) => {
      setBusy(true);
      setError(false);
      try {
        await action();
        await refresh();
      } catch (reason) {
        if (reason instanceof VigilRevisionConflictError) {
          await refresh();
        } else {
          handleError(reason);
        }
      } finally {
        setBusy(false);
      }
    },
    [handleError, refresh],
  );

  const selectedEmail = emails.find((email) => email.id === vault?.pending?.confirmation_email_id);

  let content;
  if (!vault) {
    content = <p>{t("loading")}</p>;
  } else if (ACTIVE_PHASES.has(vault.phase)) {
    content = (
      <div className="flex flex-col gap-3">
        <p>{t("active")}</p>
        <Button disabled={busy} onClick={() => void run(() => pauseVigilVault(vault.revision))}>
          {t("pause")}
        </Button>
      </div>
    );
  } else if (vault.phase === "RELEASED" || vault.phase === "REVOKED") {
    content = <p>{t("terminal")}</p>;
  } else if (!vault.pending) {
    content = (
      <div className="flex flex-col gap-3">
        <p>{t("noCandidate")}</p>
        <Button render={<Link href="/vigil/setup" />}>{t("goToSetup")}</Button>
      </div>
    );
  } else if (
    vault.pending.object_status !== "ready" ||
    !vault.pending.object_id ||
    !selectedEmail
  ) {
    content = (
      <div className="flex flex-col gap-3">
        <p>{t("incomplete")}</p>
        <Button render={<Link href="/vigil/setup" />}>{t("restartSetup")}</Button>
      </div>
    );
  } else if (!vault.pending.confirmation_email_verified) {
    content = (
      <div className="flex flex-col gap-3">
        <p>{t("verificationRequired", { address: selectedEmail?.address ?? "" })}</p>
        <Button
          disabled={busy}
          onClick={() =>
            void run(() =>
              sendVigilConfirmationEmailVerification(
                vault.pending?.confirmation_email_id ?? "",
                vault.revision,
              ),
            )
          }
        >
          {t("sendVerification")}
        </Button>
      </div>
    );
  } else {
    const candidate = vault.pending;
    content = (
      <div className="flex flex-col gap-3">
        <p>{t("ready", { address: selectedEmail?.address ?? "" })}</p>
        <Button
          disabled={busy}
          onClick={() =>
            void run(() =>
              armVigilVault({
                expected_revision: vault.revision,
                config_id: candidate.config_id,
                object_id: candidate.object_id ?? "",
              }),
            )
          }
        >
          {t("activate")}
        </Button>
      </div>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("title")}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {content}
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {t("error")}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
