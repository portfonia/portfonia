import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { getSession } = vi.hoisted(() => ({
  getSession: vi.fn(),
}));

vi.mock("@/lib/supabase/browser", () => ({
  createClient: () => ({ auth: { getSession } }),
}));
vi.mock("@/lib/vigil-origin", () => ({
  configuredVigilOrigin: () => "https://vigil.portfonia.com",
}));

import { LocaleProvider } from "@/app/_components/locale-provider";
import { VigilBridgeClient } from "./bridge-client";

const STATE = "state-from-opener";
const ORIGIN = "https://vigil.portfonia.com";

describe("VigilBridgeClient", () => {
  const originalFetch = global.fetch;

  beforeEach(() => {
    vi.clearAllMocks();
    getSession.mockResolvedValue({
      data: { session: { access_token: "at", refresh_token: "rt" } },
    });
    global.fetch = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
  });

  afterEach(() => {
    global.fetch = originalFetch;
  });

  it("sends ready to the opener and does not send session until Continue is clicked", async () => {
    const postMessage = vi.fn();
    Object.defineProperty(window, "opener", { value: { postMessage }, configurable: true });
    render(
      <LocaleProvider>
        <VigilBridgeClient email="owner@example.com" />
      </LocaleProvider>,
    );

    await waitFor(() => expect(postMessage).toHaveBeenCalled());
    expect(postMessage).toHaveBeenCalledWith({ type: "ready" }, ORIGIN);
    expect(screen.getByRole("button", { name: /continue to vigil/i })).toBeDisabled();
    expect(getSession).not.toHaveBeenCalled();
  });

  it("enables Continue after a valid request from the opener, then posts session tokens", async () => {
    const postMessage = vi.fn();
    const opener = { postMessage };
    Object.defineProperty(window, "opener", { value: opener, configurable: true });
    render(
      <LocaleProvider>
        <VigilBridgeClient email="owner@example.com" />
      </LocaleProvider>,
    );

    window.dispatchEvent(
      new MessageEvent("message", {
        origin: ORIGIN,
        source: opener as unknown as Window,
        data: { type: "request", state: STATE },
      }),
    );

    const button = await screen.findByRole("button", { name: /continue to vigil/i });
    await waitFor(() => expect(button).toBeEnabled());
    await userEvent.click(button);

    await waitFor(() =>
      expect(postMessage).toHaveBeenCalledWith(
        {
          type: "session",
          state: STATE,
          access_token: "at",
          refresh_token: "rt",
        },
        ORIGIN,
      ),
    );
    expect(global.fetch).toHaveBeenCalledWith("/api/auth/session-status", { cache: "no-store" });
  });

  it("does not send tokens when session-status is 401 at Continue", async () => {
    global.fetch = vi.fn().mockResolvedValue(new Response("", { status: 401 }));
    const assign = vi.fn();
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { assign },
    });
    const postMessage = vi.fn();
    const opener = { postMessage };
    Object.defineProperty(window, "opener", { value: opener, configurable: true });
    render(
      <LocaleProvider>
        <VigilBridgeClient email="owner@example.com" />
      </LocaleProvider>,
    );

    window.dispatchEvent(
      new MessageEvent("message", {
        origin: ORIGIN,
        source: opener as unknown as Window,
        data: { type: "request", state: STATE },
      }),
    );

    const button = await screen.findByRole("button", { name: /continue to vigil/i });
    await waitFor(() => expect(button).toBeEnabled());
    await userEvent.click(button);

    await waitFor(() => expect(assign).toHaveBeenCalledWith("/login?next=/auth/vigil"));
    expect(postMessage).not.toHaveBeenCalledWith(
      expect.objectContaining({ type: "session" }),
      ORIGIN,
    );
  });

  it("ignores a request from the wrong origin and leaves Continue disabled", async () => {
    const postMessage = vi.fn();
    const opener = { postMessage };
    Object.defineProperty(window, "opener", { value: opener, configurable: true });
    render(
      <LocaleProvider>
        <VigilBridgeClient email="owner@example.com" />
      </LocaleProvider>,
    );

    window.dispatchEvent(
      new MessageEvent("message", {
        origin: "https://evil.example",
        source: opener as unknown as Window,
        data: { type: "request", state: STATE },
      }),
    );

    expect(screen.getByRole("button", { name: /continue to vigil/i })).toBeDisabled();
  });
});
