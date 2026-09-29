"use client";

import { initializePaddle, type Paddle } from "@paddle/paddle-js";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { getCheckoutConfig, type CheckoutConfig } from "@/lib/api";

type Ready = { config: CheckoutConfig; paddle: Paddle; prices: Record<string, string> };

export function CreditPurchase() {
  const t = useTranslations("profile");
  const [state, setState] = useState<"loading" | "unavailable" | Ready>("loading");

  useEffect(() => {
    let mounted = true;
    async function load() {
      try {
        const config = await getCheckoutConfig();
        if (!config) throw new Error("checkout config unavailable");
        const paddle = await initializePaddle({ environment: config.environment, token: config.client_token });
        if (!paddle) throw new Error("Paddle unavailable");
        const preview = await paddle.PricePreview({
          items: config.packs.map((pack) => ({ priceId: pack.price_id, quantity: 1 })),
        });
        const prices = Object.fromEntries(preview.data.details.lineItems.map((item) => [
          item.price.id, item.formattedTotals.total,
        ]));
        if (mounted) setState({ config, paddle, prices });
      } catch {
        if (mounted) setState("unavailable");
      }
    }
    void load();
    return () => { mounted = false; };
  }, []);

  return (
    <div className="flex flex-col gap-3 border-t pt-4">
      <h3 className="text-sm font-semibold">{t("creditPurchaseHeading")}</h3>
      {state === "loading" && <p className="text-sm">{t("creditPurchaseLoading")}</p>}
      {state === "unavailable" && <p className="text-sm">{t("creditPurchaseUnavailable")}</p>}
      {typeof state === "object" && (
        <>
          {state.config.packs.map((pack) => (
            <div key={pack.price_id} className="flex items-center justify-between gap-3">
              <span>{t("creditPackLabel", { credits: pack.credits.replace(/\.00$/, "") })}</span>
              <span>{state.prices[pack.price_id]}</span>
              <Button onClick={() => state.paddle.Checkout.open({
                items: [{ priceId: pack.price_id, quantity: 1 }],
                customer: { email: state.config.email },
                customData: { user_id: state.config.user_id },
                settings: {
                  displayMode: "overlay", variant: "one-page",
                  successUrl: `${window.location.origin}/profile?purchase=completed`,
                },
              })}>{t("creditBuyButton")}</Button>
            </div>
          ))}
          <p className="text-xs text-muted-foreground">
            {t.rich("creditPurchaseTerms", {
              terms: (children) => <Link href="/terms">{children}</Link>,
              refund: (children) => <Link href="/refund">{children}</Link>,
            })}
          </p>
        </>
      )}
    </div>
  );
}
