"use client";

import { useTranslations } from "next-intl";

import type { Messages } from "@/locales";

export function PlanComparison() {
  const t = useTranslations("pricingPlans");
  const heading: string = t.raw("heading");
  const plans: Messages["pricingPlans"]["plans"] = t.raw("plans");
  const footnote: string = t.raw("footnote");

  return (
    <section aria-labelledby="plan-comparison-heading" className="mt-10">
      <h2 id="plan-comparison-heading" className="font-serif text-lg">{heading}</h2>
      <div className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-3">
        {plans.map((plan, i) => (
          <section
            key={plan.name}
            aria-labelledby={`plan-${i}`}
            data-plan={["briefing", "advanced", "jade"][i]}
            className="flex flex-col overflow-hidden rounded-lg border border-foreground/10"
          >
            <h3 id={`plan-${i}`} className={i === 2 ? "jade-surface px-4 py-3 font-serif text-lg" : "px-4 py-3 font-serif text-lg"}>{plan.name}</h3>
            <div className="flex flex-col gap-3 px-4 pb-4 pt-3">
              {plan.prices.map(price => <p key={price} className="text-sm font-medium">{price}</p>)}
              {plan.lead !== "" && <p className="text-sm text-foreground/70">{plan.lead}</p>}
              <ul className="flex list-disc flex-col gap-2 pl-5 text-sm leading-relaxed text-foreground/70">
                {plan.features.map(feature => <li key={feature}>{feature}</li>)}
              </ul>
            </div>
          </section>
        ))}
      </div>
      <p className="mt-3 text-xs text-foreground/45">{footnote}</p>
    </section>
  );
}
