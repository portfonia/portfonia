"use client";

import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useVigilSetup, type VigilSetupStage } from "./_lib/use-vigil-setup";
import {
  VIGIL_GRACE_HOURS_MAX,
  VIGIL_GRACE_HOURS_MIN,
  VIGIL_INTERVAL_DAYS_OPTIONS,
  VIGIL_MAX_RECIPIENTS,
  VIGIL_MIN_RECIPIENTS,
  type SetupRecipientForm,
} from "./_lib/validation";

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function stageLabel(t: ReturnType<typeof useTranslations>, stage: VigilSetupStage): string | null {
  if (!stage) return null;
  return t(`setup.stage.${stage}`);
}

export function VigilSetupPageBody() {
  const t = useTranslations("vigil");
  const s = useVigilSetup();

  const submitting = s.phase === "submitting";
  const showForm = s.phase === "form" || s.phase === "submitting" || s.phase === "error";

  function updateRecipient(index: number, patch: Partial<SetupRecipientForm>) {
    s.setRecipients(s.recipients.map((r, i) => (i === index ? { ...r, ...patch } : r)));
  }

  function addRecipient() {
    if (s.recipients.length >= VIGIL_MAX_RECIPIENTS) return;
    s.setRecipients([...s.recipients, { email: "", email_confirm: "" }]);
  }

  function removeRecipient(index: number) {
    if (s.recipients.length <= VIGIL_MIN_RECIPIENTS) return;
    s.setRecipients(s.recipients.filter((_, i) => i !== index));
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("title")}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-6">
        {s.vaultStatus && s.vaultStatus.phase !== "DISARMED" && (
          <p className="rounded-lg border border-border bg-muted px-3 py-2 text-sm text-foreground/80">
            {t("setup.activeStillReleasingNote")}
          </p>
        )}
        {s.vaultStatus?.pending && (
          <p className="rounded-lg border border-border bg-muted px-3 py-2 text-sm text-foreground/80">
            {t("setup.pendingExistsNote")}
          </p>
        )}

        {s.phase === "ready" && (
          <div className="flex flex-col gap-3">
            <h3 className="text-base font-medium">{t("setup.ready.title")}</h3>
            <p className="text-sm text-foreground/80">{t("setup.ready.body")}</p>
            {s.armed ? (
              <p className="text-sm text-foreground/80">{t("setup.drill.armed")}</p>
            ) : (
              <>
                {s.drillUiState !== "idle" && (
                  <p className="text-sm text-foreground/80">{t(`setup.drill.${s.drillUiState}`)}</p>
                )}
                {s.drillUiState === "idle" || s.drillUiState === "expired" ? (
                  <Button type="button" onClick={() => void s.sendDrill()}>
                    {t("setup.drill.send")}
                  </Button>
                ) : null}
                {s.drillUiState === "confirmed" ? (
                  <Button type="button" onClick={() => void s.activate()}>
                    {t("setup.drill.activate")}
                  </Button>
                ) : null}
                {s.errorCode && (
                  <p className="text-sm text-destructive" role="alert">
                    {t.has(`setup.errors.${s.errorCode}`)
                      ? t(`setup.errors.${s.errorCode}`)
                      : t("setup.errors.unknown")}
                  </p>
                )}
              </>
            )}
          </div>
        )}

        {showForm && (
          <form
            className="flex flex-col gap-6"
            onSubmit={(e) => {
              e.preventDefault();
              void s.submit();
            }}
          >
            <div className="flex flex-col gap-2">
              <label htmlFor="vigil-file" className="text-sm font-medium">
                {t("setup.file.label")}
              </label>
              <input
                id="vigil-file"
                type="file"
                disabled={submitting}
                onChange={(e) => s.setFile(e.target.files?.[0] ?? null)}
                className="text-sm"
              />
              <p className="text-sm text-muted-foreground">
                {s.file ? t("setup.file.selected", { name: s.file.name, size: formatFileSize(s.file.size) }) : t("setup.file.none")}
              </p>
            </div>

            <fieldset className="flex flex-col gap-3">
              <legend className="text-sm font-medium">{t("setup.password.sectionTitle")}</legend>
              <div className="flex gap-4">
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="radio"
                    name="vigil-has-password"
                    checked={s.hasPassword === true}
                    disabled={submitting}
                    onChange={() => s.setHasPassword(true)}
                  />
                  {t("setup.password.withPassword")}
                </label>
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="radio"
                    name="vigil-has-password"
                    checked={s.hasPassword === false}
                    disabled={submitting}
                    onChange={() => s.setHasPassword(false)}
                  />
                  {t("setup.password.withoutPassword")}
                </label>
              </div>
              {s.hasPassword === false && (
                <p className="text-sm text-destructive">{t("setup.password.withoutPasswordWarning")}</p>
              )}
              {s.hasPassword === true && (
                <div className="flex flex-col gap-2">
                  <input
                    type="password"
                    aria-label={t("setup.password.passwordLabel")}
                    placeholder={t("setup.password.passwordLabel")}
                    value={s.password}
                    disabled={submitting}
                    onChange={(e) => s.setPassword(e.target.value)}
                    className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm"
                  />
                  <input
                    type="password"
                    aria-label={t("setup.password.passwordConfirmLabel")}
                    placeholder={t("setup.password.passwordConfirmLabel")}
                    value={s.passwordConfirm}
                    disabled={submitting}
                    onChange={(e) => s.setPasswordConfirm(e.target.value)}
                    className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm"
                  />
                </div>
              )}
            </fieldset>

            <fieldset className="flex flex-col gap-3">
              <legend className="text-sm font-medium">{t("setup.recipients.sectionTitle")}</legend>
              {s.recipients.map((recipient, index) => (
                <div key={index} className="flex flex-col gap-2 rounded-lg border border-border p-3">
                  <input
                    type="email"
                    aria-label={t("setup.recipients.emailLabel", { n: index + 1 })}
                    placeholder={t("setup.recipients.emailLabel", { n: index + 1 })}
                    value={recipient.email}
                    disabled={submitting}
                    onChange={(e) => updateRecipient(index, { email: e.target.value })}
                    className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm"
                  />
                  <input
                    type="email"
                    aria-label={t("setup.recipients.emailConfirmLabel", { n: index + 1 })}
                    placeholder={t("setup.recipients.emailConfirmLabel", { n: index + 1 })}
                    value={recipient.email_confirm}
                    disabled={submitting}
                    onChange={(e) => updateRecipient(index, { email_confirm: e.target.value })}
                    className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm"
                  />
                  {s.recipients.length > VIGIL_MIN_RECIPIENTS && (
                    <Button type="button" variant="outline" disabled={submitting} onClick={() => removeRecipient(index)}>
                      {t("setup.recipients.remove")}
                    </Button>
                  )}
                </div>
              ))}
              {s.recipients.length < VIGIL_MAX_RECIPIENTS && (
                <Button type="button" variant="outline" disabled={submitting} onClick={addRecipient}>
                  {t("setup.recipients.add")}
                </Button>
              )}
            </fieldset>

            <div className="flex flex-col gap-2">
              <label htmlFor="vigil-message" className="text-sm font-medium">
                {t("setup.message.label")}
              </label>
              <textarea
                id="vigil-message"
                value={s.message}
                disabled={submitting}
                onChange={(e) => s.setMessage(e.target.value)}
                rows={4}
                className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm"
              />
            </div>

            <div className="flex flex-col gap-4 sm:flex-row">
              <div className="flex flex-1 flex-col gap-2">
                <label htmlFor="vigil-interval" className="text-sm font-medium">
                  {t("setup.schedule.intervalLabel")}
                </label>
                <select
                  id="vigil-interval"
                  value={s.intervalDays}
                  disabled={submitting}
                  onChange={(e) => s.setIntervalDays(Number(e.target.value))}
                  className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm"
                >
                  {VIGIL_INTERVAL_DAYS_OPTIONS.map((days) => (
                    <option key={days} value={days}>
                      {t("setup.schedule.intervalDays", { days })}
                    </option>
                  ))}
                </select>
              </div>
              <div className="flex flex-1 flex-col gap-2">
                <label htmlFor="vigil-grace" className="text-sm font-medium">
                  {t("setup.schedule.graceLabel")}
                </label>
                <input
                  id="vigil-grace"
                  type="number"
                  min={VIGIL_GRACE_HOURS_MIN}
                  max={VIGIL_GRACE_HOURS_MAX}
                  value={s.graceHours}
                  disabled={submitting}
                  onChange={(e) => s.setGraceHours(Number(e.target.value))}
                  className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm"
                />
              </div>
            </div>

            {s.validationErrors.length > 0 && (
              <ul className="flex flex-col gap-1" role="alert">
                {s.validationErrors.map((code) => (
                  <li key={code} className="text-sm text-destructive">
                    {t(`setup.errors.${code}`)}
                  </li>
                ))}
              </ul>
            )}

            {s.phase === "error" && s.errorCode && (
              <p className="text-sm text-destructive" role="alert">
                {t.has(`setup.errors.${s.errorCode}`) ? t(`setup.errors.${s.errorCode}`) : t("setup.errors.unknown")}
              </p>
            )}

            {submitting && (
              <div className="flex flex-col gap-1">
                <p className="text-sm text-muted-foreground">{stageLabel(t, s.stage)}</p>
                {s.stage === "uploading" && (
                  <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
                    <div
                      className="h-full bg-primary transition-all"
                      style={{ width: `${Math.round(s.progress * 100)}%` }}
                    />
                  </div>
                )}
              </div>
            )}

            <div className="flex items-center gap-3">
              {submitting ? (
                <Button type="button" variant="outline" onClick={s.cancel}>
                  {t("setup.cancel")}
                </Button>
              ) : (
                <Button type="submit">{s.phase === "error" ? t("setup.retry") : t("setup.submit")}</Button>
              )}
            </div>
          </form>
        )}
      </CardContent>
    </Card>
  );
}
