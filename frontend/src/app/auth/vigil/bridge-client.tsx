"use client";

import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useTranslations } from "next-intl";

import { createClient } from "@/lib/supabase/browser";
import { configuredVigilOrigin } from "@/lib/vigil-origin";
import { HANDSHAKE_TTL_MS, parseHandshakeMessage } from "@/lib/vigil-handoff";

function subscribeNoop() {
  return () => {};
}

export function VigilBridgeClient({ email }: { email: string }) {
  const t = useTranslations("auth");
  const hasOpener = useSyncExternalStore(
    subscribeNoop,
    () => window.opener !== null,
    () => false,
  );
  const [handshakeState, setHandshakeState] = useState<string | null>(null);
  const [errorKey, setErrorKey] = useState<"vigilBridgeNoOpener" | "vigilBridgeFailed" | null>(
    null,
  );
  const sentReady = useRef(false);
  const shownError = errorKey ?? (hasOpener ? null : "vigilBridgeNoOpener");

  useEffect(() => {
    const opener = window.opener;
    if (opener === null) {
      return;
    }
    const origin = configuredVigilOrigin();
    const onMessage = (event: MessageEvent) => {
      if (event.origin !== origin) return;
      if (event.source !== opener) return;
      const message = parseHandshakeMessage(event.data);
      if (message === null || message.type !== "request") return;
      setHandshakeState(message.state);
    };
    window.addEventListener("message", onMessage);
    if (!sentReady.current) {
      opener.postMessage({ type: "ready" }, origin);
      sentReady.current = true;
    }
    const timeout = window.setTimeout(() => {
      window.removeEventListener("message", onMessage);
      setHandshakeState(null);
      setErrorKey("vigilBridgeFailed");
    }, HANDSHAKE_TTL_MS);
    return () => {
      window.clearTimeout(timeout);
      window.removeEventListener("message", onMessage);
    };
  }, []);

  async function continueToVigil() {
    const opener = window.opener;
    if (opener === null || handshakeState === null) {
      setErrorKey("vigilBridgeFailed");
      return;
    }
    let sessionActive = false;
    try {
      const res = await fetch("/api/auth/session-status", { cache: "no-store" });
      if (res.status === 401) {
        window.location.assign("/login?next=/auth/vigil");
        return;
      }
      sessionActive = res.status === 204;
    } catch {
      sessionActive = false;
    }
    if (!sessionActive) {
      setErrorKey("vigilBridgeFailed");
      return;
    }
    const origin = configuredVigilOrigin();
    const supabase = createClient();
    const {
      data: { session },
    } = await supabase.auth.getSession();
    if (!session?.access_token || !session.refresh_token) {
      setErrorKey("vigilBridgeFailed");
      return;
    }
    opener.postMessage(
      {
        type: "session",
        state: handshakeState,
        access_token: session.access_token,
        refresh_token: session.refresh_token,
      },
      origin,
    );
  }

  return (
    <main className="mx-auto flex max-w-lg flex-col gap-6 px-4 py-24">
      <h1 className="font-serif text-3xl">{t("vigilBridgeHeading")}</h1>
      <p className="text-sm text-foreground/80">{t("vigilBridgeSignedInAs", { email })}</p>
      {shownError ? (
        <p className="text-sm text-destructive" role="alert">
          {t(shownError)}
        </p>
      ) : null}
      <button
        type="button"
        disabled={handshakeState === null || shownError !== null}
        onClick={() => void continueToVigil()}
        className="rounded-md bg-foreground px-4 py-2 text-sm text-background disabled:opacity-50"
      >
        {t("vigilBridgeContinue")}
      </button>
      {handshakeState === null && shownError === null ? (
        <p className="text-sm text-foreground/60">{t("vigilBridgeWorking")}</p>
      ) : null}
    </main>
  );
}
