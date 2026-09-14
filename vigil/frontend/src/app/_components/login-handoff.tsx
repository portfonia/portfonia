"use client";

import { useRef, useState } from "react";

import { createClient } from "@/lib/supabase/browser";
import {
  HANDSHAKE_TTL_MS,
  createStateTicket,
  generateHandshakeState,
  type HandshakeEvent,
} from "@/lib/handoff";
import { configuredPortfoniaOrigin } from "@/lib/portfonia-origin";

function currentTimeMs(): number {
  return Date.now();
}

type Failure = "blocked" | "closed" | "failed";

const FAILURE_COPY: Record<Failure, string> = {
  blocked: "The login popup was blocked. Allow popups for this site and retry.",
  closed: "The login window was closed before Vigil could finish. Retry when ready.",
  failed: "Login did not complete. Retry from this page. No weaker fallback is available.",
};

export function LoginHandoff() {
  const [failure, setFailure] = useState<Failure | null>(null);
  const [busy, setBusy] = useState(false);
  const ticketRef = useRef<ReturnType<typeof createStateTicket> | null>(null);
  const popupRef = useRef<Window | null>(null);
  const listenerRef = useRef<((event: MessageEvent) => void) | null>(null);
  const timersRef = useRef<{ poll?: number; timeout?: number }>({});

  function disarm() {
    if (listenerRef.current) {
      window.removeEventListener("message", listenerRef.current);
      listenerRef.current = null;
    }
    ticketRef.current?.clear();
    ticketRef.current = null;
    popupRef.current = null;
    if (timersRef.current.poll !== undefined) {
      window.clearInterval(timersRef.current.poll);
    }
    if (timersRef.current.timeout !== undefined) {
      window.clearTimeout(timersRef.current.timeout);
    }
    timersRef.current = {};
    setBusy(false);
  }

  function onLoginClick() {
    disarm();
    setFailure(null);
    const origin = configuredPortfoniaOrigin();
    const state = generateHandshakeState();
    const popup = window.open(`${origin}/auth/vigil`, "portfonia-vigil-handoff", "popup");
    if (!popup) {
      setFailure("blocked");
      return;
    }
    popupRef.current = popup;
    const ticket = createStateTicket({
      state,
      expectedOrigin: origin,
      expectedSource: popup,
      createdAtMs: currentTimeMs(),
    });
    ticketRef.current = ticket;

    const onMessage = (event: MessageEvent) => {
      void acceptMessage(event, { ticket, popup, origin, state });
    };
    listenerRef.current = onMessage;
    window.addEventListener("message", onMessage);

    timersRef.current.poll = window.setInterval(() => {
      if (popup.closed) {
        disarm();
        setFailure("closed");
      }
    }, 400);
    timersRef.current.timeout = window.setTimeout(() => {
      if (!popup.closed) popup.close();
      disarm();
      setFailure("failed");
    }, HANDSHAKE_TTL_MS);
    setBusy(true);
  }

  async function acceptMessage(
    event: MessageEvent,
    ctx: {
      ticket: ReturnType<typeof createStateTicket>;
      popup: Window;
      origin: string;
      state: string;
    },
  ) {
    const envelope: HandshakeEvent = {
      origin: event.origin,
      source: event.source,
      data: event.data,
    };
    const result = ctx.ticket.consume(envelope);
    if (!result.ok) return;
    if (result.message.type === "ready") {
      ctx.popup.postMessage({ type: "request", state: ctx.state }, ctx.origin);
      return;
    }
    if (result.message.type !== "session") return;
    disarm();
    ctx.popup.close();
    const supabase = createClient();
    const { error } = await supabase.auth.setSession({
      access_token: result.message.access_token,
      refresh_token: result.message.refresh_token,
    });
    if (error) {
      setFailure("failed");
      return;
    }
    window.location.assign("/");
  }

  return (
    <main className="mx-auto flex max-w-lg flex-col gap-6 px-6 py-24">
      <h1 className="text-3xl">Vigil</h1>
      <p className="text-sm opacity-80">Sign in with your Portfonia account to manage this vault.</p>
      <button
        type="button"
        onClick={onLoginClick}
        disabled={busy}
        className="w-fit rounded-md bg-zinc-100 px-4 py-2 text-sm text-zinc-900"
      >
        Log in with Portfonia
      </button>
      {failure ? (
        <p className="text-sm text-red-400" role="alert">
          {FAILURE_COPY[failure]}
        </p>
      ) : null}
    </main>
  );
}
