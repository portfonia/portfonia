"use client";

// One-question-per-step wizard (Ring 1-B design.md §8.3: "一页一题、选择题为主、
// 最后一步自由文本"). Every question is pre-filled from Concept §4.3's default
// investment philosophy (structural-growth tilt, long horizon, balanced risk,
// macro/geopolitics-aware) — a user can click straight through to Save without
// changing anything, which is what "可跳过" (§8.6) means in practice: the
// per-field defaults ARE the skip path. A separate "Skip for now" link leaves
// without submitting at all, for a user who doesn't want to answer today.
import Link from "next/link";
import { useState } from "react";
import { useTranslations } from "next-intl";

import {
  ApiError,
  putInvestmentContext,
  type InvestmentContext,
  type Questionnaire,
} from "@/lib/api";
import { Button } from "@/components/ui/button";

// Concept §4.3's five default-philosophy directions, translated into this
// questionnaire's closed enums (a product judgment call, not a literal
// mapping — §4.3 does not specify asset_scale/markets/risk_appetite at all,
// so those default to the most neutral option in their enum).
const DEFAULT_QUESTIONNAIRE: Questionnaire = {
  asset_scale: "100K_500K",
  markets: [],
  style: "GROWTH",
  horizon: "LONG",
  risk_appetite: "BALANCED",
  sectors_of_interest: ["Technology"],
  objective: "GROWTH",
  intel_focus: "MACRO",
};

type SingleDim = "asset_scale" | "style" | "horizon" | "risk_appetite" | "objective" | "intel_focus";
type MultiDim = "markets" | "sectors_of_interest";

const SINGLE_STEPS: SingleDim[] = [
  "asset_scale",
  "style",
  "horizon",
  "risk_appetite",
  "objective",
  "intel_focus",
];
const MULTI_STEPS: MultiDim[] = ["markets", "sectors_of_interest"];

// Display order mirrors Ring 1-B design.md §8.3's table.
const STEP_ORDER: (SingleDim | MultiDim)[] = [
  "asset_scale",
  "markets",
  "style",
  "horizon",
  "risk_appetite",
  "sectors_of_interest",
  "objective",
  "intel_focus",
];
const TOTAL_STEPS = STEP_ORDER.length + 1; // + free-text step

function OptionButton({
  label,
  selected,
  onClick,
}: {
  label: string;
  selected: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={selected}
      className={`w-full rounded-lg border px-4 py-2.5 text-left text-sm transition-colors ${
        selected
          ? "border-primary bg-primary/10 text-foreground"
          : "border-border bg-background text-foreground/80 hover:bg-muted"
      }`}
    >
      {label}
    </button>
  );
}

export function QuestionnaireForm({
  initialContext,
}: {
  initialContext: InvestmentContext | null;
}) {
  const t = useTranslations("questionnaire");
  const [answers, setAnswers] = useState<Questionnaire>(
    initialContext?.questionnaire ?? DEFAULT_QUESTIONNAIRE,
  );
  const [freeText, setFreeText] = useState(initialContext?.free_text ?? "");
  const [step, setStep] = useState(0);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const isFreeTextStep = step === STEP_ORDER.length;
  const dim = isFreeTextStep ? null : STEP_ORDER[step];

  function selectSingle(field: SingleDim, value: string) {
    setAnswers((prev) => ({ ...prev, [field]: value }) as Questionnaire);
  }

  function toggleMulti(field: MultiDim, value: string) {
    setAnswers((prev) => {
      // `value` always comes from this field's own option keys
      // (t.raw(`dims.${field}.options`), rendered below) — the union member
      // is validated by construction, not by this cast.
      const current: string[] = prev[field];
      const next = current.includes(value)
        ? current.filter((v) => v !== value)
        : [...current, value];
      return { ...prev, [field]: next } as Questionnaire;
    });
  }

  async function handleSave() {
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      await putInvestmentContext(answers, freeText.trim() === "" ? null : freeText);
      setSaved(true);
      // Re-entering /questionnaire from the menu while already on that route
      // is a same-path Link click — Next.js treats it as a no-op (no
      // remount), so nothing else would ever reset `step` back to the start
      // (issue #214).
      setStep(0);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t("errorSaveFailed"));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between text-sm text-muted-foreground">
        <span>{t("stepOf", { current: step + 1, total: TOTAL_STEPS })}</span>
        <Link href="/" className="underline-offset-4 hover:underline">
          {t("skip")}
        </Link>
      </div>

      {dim && SINGLE_STEPS.includes(dim as SingleDim) && (
        <fieldset className="flex flex-col gap-3">
          <legend className="mb-1 text-base font-medium">
            {t(`dims.${dim}.question`)}
          </legend>
          {Object.entries(t.raw(`dims.${dim}.options`) as Record<string, string>).map(
            ([value, label]) => (
              <OptionButton
                key={value}
                label={label}
                selected={answers[dim as SingleDim] === value}
                onClick={() => selectSingle(dim as SingleDim, value)}
              />
            ),
          )}
        </fieldset>
      )}

      {dim && MULTI_STEPS.includes(dim as MultiDim) && (
        <fieldset className="flex flex-col gap-3">
          <legend className="mb-1 text-base font-medium">
            {t(`dims.${dim}.question`)}
          </legend>
          {Object.entries(t.raw(`dims.${dim}.options`) as Record<string, string>).map(
            ([value, label]) => (
              <OptionButton
                key={value}
                label={label}
                selected={(answers[dim as MultiDim] as string[]).includes(value)}
                onClick={() => toggleMulti(dim as MultiDim, value)}
              />
            ),
          )}
        </fieldset>
      )}

      {isFreeTextStep && (
        <div className="flex flex-col gap-2">
          <label htmlFor="free-text" className="text-base font-medium">
            {t("freeTextHeading")}
          </label>
          <p className="text-sm text-muted-foreground">{t("freeTextHint")}</p>
          <textarea
            id="free-text"
            value={freeText}
            onChange={(e) => setFreeText(e.target.value)}
            placeholder={t("freeTextPlaceholder")}
            rows={6}
            className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm"
          />
        </div>
      )}

      {error && (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      )}
      {saved && <p className="text-sm text-foreground/70">{t("saved")}</p>}

      <div className="flex items-center justify-between">
        <Button
          type="button"
          variant="outline"
          disabled={step === 0}
          onClick={() => setStep((s) => Math.max(0, s - 1))}
        >
          {t("back")}
        </Button>
        {isFreeTextStep ? (
          <Button type="button" disabled={saving} onClick={() => void handleSave()}>
            {saving ? t("saving") : t("save")}
          </Button>
        ) : (
          <Button type="button" onClick={() => setStep((s) => Math.min(TOTAL_STEPS - 1, s + 1))}>
            {t("next")}
          </Button>
        )}
      </div>
    </div>
  );
}
