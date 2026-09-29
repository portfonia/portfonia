"use client";

import { initializePaddle, type Paddle, type PaddleEventData } from "@paddle/paddle-js";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { getCheckoutConfig, getPurchaseStatus, type CheckoutConfig } from "@/lib/api";

type Ready = { config: CheckoutConfig; paddle: Paddle; prices: Record<string, string> };
type Notice = "openFailed" | "pending" | { credited: string } | "delayed" | null;

const OPEN_TIMEOUT_MS = 15_000;
const POLL_INTERVAL_MS = 3_000;
const POLL_LIMIT_MS = 120_000;

// initializePaddle replaces eventCallback on every call. A load started by
// an earlier mount can finish last and install its callback, so this ref
// has to outlive that mount and point at the handler installed now.
const handlerRef: { current: (event: PaddleEventData) => void } = {
  current: () => {},
};

export function CreditPurchase() {
  const t = useTranslations("profile");
  const router = useRouter();
  const [state, setState] = useState<"loading" | "unavailable" | Ready>("loading");
  const [opening, setOpening] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice>(null);
  const openingRef = useRef<string | null>(null);
  const paddleRef = useRef<Paddle | null>(null);
  const openTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pollDeadlineRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pollGeneration = useRef(0);

  function clearOpenTimer() {
    if (openTimerRef.current !== null) {
      clearTimeout(openTimerRef.current);
      openTimerRef.current = null;
    }
  }

  function stopPolling() {
    if (pollTimerRef.current !== null) {
      clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    if (pollDeadlineRef.current !== null) {
      clearTimeout(pollDeadlineRef.current);
      pollDeadlineRef.current = null;
    }
  }

  function setOpeningState(priceId: string | null) {
    openingRef.current = priceId;
    setOpening(priceId);
  }

  function startPolling(transactionId: string) {
    stopPolling();
    const generation = ++pollGeneration.current;

    async function tick() {
      const status = await getPurchaseStatus(transactionId);
      if (generation !== pollGeneration.current) return;
      if (status?.credited && status.credits != null) {
        pollGeneration.current += 1;
        stopPolling();
        setNotice({ credited: status.credits.replace(/\.00$/, "") });
        router.refresh();
        return;
      }
      pollTimerRef.current = setTimeout(() => {
        void tick();
      }, POLL_INTERVAL_MS);
    }

    void tick();
    pollDeadlineRef.current = setTimeout(() => {
      if (generation !== pollGeneration.current) return;
      pollGeneration.current += 1;
      stopPolling();
      setNotice("delayed");
    }, POLL_LIMIT_MS);
  }

  useEffect(() => {
    handlerRef.current = (event) => {
      if (event.name === "checkout.loaded" || event.name === "checkout.closed") {
        clearOpenTimer();
        setOpeningState(null);
        return;
      }
      if (event.name === "checkout.error") {
        clearOpenTimer();
        setOpeningState(null);
        setNotice("openFailed");
        return;
      }
      if (event.name !== "checkout.completed") return;
      const transactionId = event.data?.transaction_id;
      if (!transactionId?.startsWith("txn_")) return;
      clearOpenTimer();
      setOpeningState(null);
      paddleRef.current?.Checkout.close();
      setNotice("pending");
      startPolling(transactionId);
    };
  });

  useEffect(() => {
    let mounted = true;
    async function load() {
      try {
        const config = await getCheckoutConfig();
        if (!config) throw new Error("checkout config unavailable");
        const paddle = await initializePaddle({
          environment: config.environment,
          token: config.client_token,
          eventCallback: (event) => {
            handlerRef.current(event);
          },
        });
        if (!paddle) throw new Error("Paddle unavailable");
        paddleRef.current = paddle;
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
    return () => {
      mounted = false;
      handlerRef.current = () => {};
      pollGeneration.current += 1;
      clearOpenTimer();
      stopPolling();
    };
  }, []);

  function buy(priceId: string) {
    if (typeof state !== "object" || openingRef.current !== null) return;
    setOpeningState(priceId);
    setNotice(null);
    clearOpenTimer();
    openTimerRef.current = setTimeout(() => {
      if (openingRef.current === null) return;
      setOpeningState(null);
      setNotice("openFailed");
    }, OPEN_TIMEOUT_MS);
    state.paddle.Checkout.open({
      items: [{ priceId, quantity: 1 }],
      customer: { email: state.config.email },
      customData: { user_id: state.config.user_id },
      settings: { displayMode: "overlay", variant: "one-page" },
    });
  }

  const noticeText = notice === "openFailed"
    ? t("creditPurchaseOpenFailed")
    : notice === "pending"
      ? t("creditPurchasePending")
      : notice === "delayed"
        ? t("creditPurchaseDelayed")
        : notice
          ? t("creditPurchaseCredited", { credits: notice.credited })
          : null;

  return (
    <div className="flex flex-col gap-3 border-t pt-4">
      <h3 className="text-sm font-semibold">{t("creditPurchaseHeading")}</h3>
      {noticeText && <p role="status" className="text-sm">{noticeText}</p>}
      {state === "loading" && <p className="text-sm">{t("creditPurchaseLoading")}</p>}
      {state === "unavailable" && <p className="text-sm">{t("creditPurchaseUnavailable")}</p>}
      {typeof state === "object" && (
        <>
          {state.config.packs.map((pack) => (
            <div key={pack.price_id} className="flex items-center justify-between gap-3">
              <span>{t("creditPackLabel", { credits: pack.credits.replace(/\.00$/, "") })}</span>
              <span>{state.prices[pack.price_id]}</span>
              <Button
                disabled={opening !== null}
                onClick={() => buy(pack.price_id)}
              >
                {opening === pack.price_id ? t("creditPurchaseOpening") : t("creditBuyButton")}
              </Button>
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
