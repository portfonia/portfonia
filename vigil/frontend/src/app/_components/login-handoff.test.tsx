import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { setSession } = vi.hoisted(() => ({
  setSession: vi.fn(),
}));

vi.mock("@/lib/supabase/browser", () => ({
  createClient: () => ({ auth: { setSession } }),
}));
vi.mock("@/lib/portfonia-origin", () => ({
  configuredPortfoniaOrigin: () => "https://portfonia.com",
}));

import { LoginHandoff } from "./login-handoff";

const ORIGIN = "https://portfonia.com";

describe("LoginHandoff", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setSession.mockResolvedValue({ error: null });
  });

  it("does not open a popup until the user clicks", () => {
    const open = vi.fn();
    window.open = open;
    render(<LoginHandoff />);
    expect(open).not.toHaveBeenCalled();
  });

  it("opens the Portfonia bridge as a popup from the click", async () => {
    const popup = { closed: false, close: vi.fn(), postMessage: vi.fn() };
    window.open = vi.fn(() => popup as unknown as Window);
    render(<LoginHandoff />);
    await userEvent.click(screen.getByRole("button", { name: /log in with portfonia/i }));
    expect(window.open).toHaveBeenCalledWith(
      "https://portfonia.com/auth/vigil",
      "portfonia-vigil-handoff",
      "popup",
    );
  });

  it("shows a retryable message when the popup is blocked", async () => {
    window.open = vi.fn(() => null);
    render(<LoginHandoff />);
    await userEvent.click(screen.getByRole("button", { name: /log in with portfonia/i }));
    expect(screen.getByRole("alert").textContent).toMatch(/blocked/i);
  });

  it("answers a valid ready with request, then sets the host session from a session message", async () => {
    const popup = { closed: false, close: vi.fn(), postMessage: vi.fn() };
    window.open = vi.fn(() => popup as unknown as Window);
    const assign = vi.fn();
    Object.defineProperty(window, "location", {
      value: { ...window.location, assign },
      writable: true,
    });

    render(<LoginHandoff />);
    await userEvent.click(screen.getByRole("button", { name: /log in with portfonia/i }));

    window.dispatchEvent(
      new MessageEvent("message", {
        origin: ORIGIN,
        source: popup as unknown as Window,
        data: { type: "ready" },
      }),
    );
    await waitFor(() =>
      expect(popup.postMessage).toHaveBeenCalledWith(
        expect.objectContaining({ type: "request", state: expect.any(String) }),
        ORIGIN,
      ),
    );
    const request = popup.postMessage.mock.calls[0] as [{ state: string }, string];
    const state = request[0].state;

    window.dispatchEvent(
      new MessageEvent("message", {
        origin: ORIGIN,
        source: popup as unknown as Window,
        data: {
          type: "session",
          state,
          access_token: "at",
          refresh_token: "rt",
        },
      }),
    );

    await waitFor(() =>
      expect(setSession).toHaveBeenCalledWith({
        access_token: "at",
        refresh_token: "rt",
      }),
    );
    expect(String(window.open)).not.toContain("access_token");
  });

  it("does not set a session for a message from the wrong origin", async () => {
    const popup = { closed: false, close: vi.fn(), postMessage: vi.fn() };
    window.open = vi.fn(() => popup as unknown as Window);
    render(<LoginHandoff />);
    await userEvent.click(screen.getByRole("button", { name: /log in with portfonia/i }));

    window.dispatchEvent(
      new MessageEvent("message", {
        origin: "https://evil.example",
        source: popup as unknown as Window,
        data: {
          type: "session",
          state: "whatever",
          access_token: "at",
          refresh_token: "rt",
        },
      }),
    );

    await new Promise((r) => setTimeout(r, 20));
    expect(setSession).not.toHaveBeenCalled();
  });
});
