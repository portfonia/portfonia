import { render, screen, waitFor, act } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";

const { usePathname, getUser, onAuthStateChange, unsubscribe } = vi.hoisted(() => ({
  usePathname: vi.fn(() => "/"),
  getUser: vi.fn(),
  onAuthStateChange: vi.fn(),
  unsubscribe: vi.fn(),
}));

vi.mock("next/navigation", () => ({ usePathname }));

vi.mock("@/lib/supabase/browser", () => ({
  createClient: () => ({
    auth: {
      getUser,
      onAuthStateChange: (cb: (...args: unknown[]) => void) => {
        onAuthStateChange(cb);
        return { data: { subscription: { unsubscribe } } };
      },
    },
  }),
}));

import {
  useSession,
  markPendingLogin,
  markOptimisticLogout,
  clearPendingLogin,
  revalidateSession,
  __resetSessionSignalsForTests,
} from "./use-session";

// Renders the hook state so each assertion can see exactly what a consumer
// (the Get Started menu) would render for the current session truth.
function Probe() {
  const session = useSession();
  if (session.status === "checking") {
    if (session.pendingReason === "login") {
      return <div data-testid="session-state">checking:login</div>;
    }
    return null;
  }
  return (
    <div data-testid="session-state">
      {session.status}
      {session.status === "authed" ? `:${session.email}` : ""}
    </div>
  );
}

function lastAuthCallback(): (...args: unknown[]) => void {
  expect(onAuthStateChange).toHaveBeenCalled();
  const calls = onAuthStateChange.mock.calls;
  return calls[calls.length - 1][0];
}

describe("useSession", () => {
  beforeEach(() => {
    __resetSessionSignalsForTests();
    vi.clearAllMocks();
    usePathname.mockReturnValue("/");
    vi.spyOn(console, "warn").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.restoreAllMocks();
    // Unconditional, not just at the end of the fake-timer tests' happy
    // path — an assertion failure there would otherwise leak fake timers
    // into every later test in this file (PR #215 review nit).
    vi.useRealTimers();
  });

  it("renders nothing while the verified check is in flight", () => {
    getUser.mockReturnValue(new Promise(() => {})); // never resolves
    const { container } = render(<Probe />);

    expect(container).toBeEmptyDOMElement();
  });

  it("reports authed with the email when getUser verifies a session", async () => {
    getUser.mockResolvedValue({ data: { user: { email: "a@b.com" } } });
    render(<Probe />);

    expect(await screen.findByTestId("session-state")).toHaveTextContent(
      "authed:a@b.com",
    );
  });

  it("reports guest when getUser verifies no session", async () => {
    getUser.mockResolvedValue({ data: { user: null } });
    render(<Probe />);

    expect(await screen.findByTestId("session-state")).toHaveTextContent("guest");
  });

  it("reports guest instead of hanging blank when getUser rejects (D2)", async () => {
    getUser.mockRejectedValue(new Error("network down"));
    render(<Probe />);

    expect(await screen.findByTestId("session-state")).toHaveTextContent("guest");
    expect(console.warn).toHaveBeenCalled();
  });

  it("never trusts an INITIAL_SESSION event carrying a stale local session (D1)", async () => {
    getUser.mockResolvedValue({ data: { user: null } }); // verified: revoked
    render(<Probe />);
    await screen.findByTestId("session-state");

    act(() => {
      lastAuthCallback()("INITIAL_SESSION", {
        user: { email: "stale@b.com" },
      });
    });

    await waitFor(() =>
      expect(screen.getByTestId("session-state")).toHaveTextContent("guest"),
    );
    expect(screen.getByTestId("session-state").textContent).not.toContain(
      "stale@b.com",
    );
  });

  it("flips to guest on a SIGNED_OUT event after being authed", async () => {
    getUser.mockResolvedValue({ data: { user: { email: "a@b.com" } } });
    render(<Probe />);
    await screen.findByTestId("session-state");

    act(() => {
      lastAuthCallback()("SIGNED_OUT", null);
    });

    await waitFor(() =>
      expect(screen.getByTestId("session-state")).toHaveTextContent("guest"),
    );
  });

  it("re-verifies on USER_UPDATED rather than trusting the event payload", async () => {
    getUser
      .mockResolvedValueOnce({ data: { user: { email: "old@b.com" } } })
      .mockResolvedValueOnce({ data: { user: { email: "new@b.com" } } });
    render(<Probe />);
    await screen.findByTestId("session-state");
    expect(screen.getByTestId("session-state")).toHaveTextContent("authed:old@b.com");

    act(() => {
      lastAuthCallback()("USER_UPDATED", {
        user: { email: "spoofed@b.com" },
      });
    });

    await waitFor(() =>
      expect(screen.getByTestId("session-state")).toHaveTextContent(
        "authed:new@b.com",
      ),
    );
    expect(screen.getByTestId("session-state").textContent).not.toContain(
      "spoofed@b.com",
    );
  });

  it("recovers to authed after logout-then-login in the same tab (SIGNED_IN re-verifies)", async () => {
    getUser
      .mockResolvedValueOnce({ data: { user: { email: "a@b.com" } } })
      .mockResolvedValueOnce({ data: { user: { email: "a@b.com" } } });
    render(<Probe />);
    await screen.findByTestId("session-state");

    act(() => {
      lastAuthCallback()("SIGNED_OUT", null);
    });
    await waitFor(() =>
      expect(screen.getByTestId("session-state")).toHaveTextContent("guest"),
    );

    act(() => {
      lastAuthCallback()("SIGNED_IN", { user: { email: "a@b.com" } });
    });

    await waitFor(() =>
      expect(screen.getByTestId("session-state")).toHaveTextContent(
        "authed:a@b.com",
      ),
    );
  });

  it("re-verifies when the tab becomes visible or focused again (one shared call for simultaneous triggers)", async () => {
    getUser.mockResolvedValue({ data: { user: { email: "a@b.com" } } });
    render(<Probe />);
    await screen.findByTestId("session-state");
    expect(getUser).toHaveBeenCalledTimes(1);

    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => "visible",
    });
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
      window.dispatchEvent(new Event("focus"));
    });

    // focus + visibilitychange fire together on tab return; the in-flight
    // guard collapses them into ONE network call.
    await waitFor(() => expect(getUser).toHaveBeenCalledTimes(2));
  });

  it("does not treat hidden-again events as revalidation triggers", async () => {
    getUser.mockResolvedValue({ data: { user: { email: "a@b.com" } } });
    render(<Probe />);
    await screen.findByTestId("session-state");
    expect(getUser).toHaveBeenCalledTimes(1);

    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => "hidden",
    });
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });

    await waitFor(() => expect(getUser).toHaveBeenCalledTimes(1));
  });

  it("does not let an in-flight verify() override a SIGNED_OUT event", async () => {
    // getUser() hangs until we release it — after SIGNED_OUT has arrived.
    let release!: (value: unknown) => void;
    getUser.mockReturnValue(
      new Promise((resolve) => {
        release = resolve;
      }),
    );
    render(<Probe />);

    act(() => {
      lastAuthCallback()("SIGNED_OUT", null);
    });
    await screen.findByTestId("session-state");

    act(() => {
      release({ data: { user: { email: "stale@b.com" } } });
    });

    expect(screen.getByTestId("session-state")).toHaveTextContent("guest");
  });

  it("shares one in-flight verify across focus and visibility triggers", async () => {
    const resolvers: Array<(v: unknown) => void> = [];
    getUser.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolvers.push(resolve);
        }),
    );
    render(<Probe />);

    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
      window.dispatchEvent(new Event("focus"));
    });

    expect(getUser).toHaveBeenCalledTimes(1);
    expect(resolvers.length).toBe(1);
    act(() => {
      resolvers[0]({ data: { user: null } });
    });
  });

  it("re-verifies when pathname changes (Server Action redirect doesn't remount SiteHeader)", async () => {
    getUser
      .mockResolvedValueOnce({ data: { user: { email: "a@b.com" } } })
      .mockResolvedValueOnce({ data: { user: null } });
    const { rerender } = render(<Probe />);
    await screen.findByTestId("session-state");
    expect(screen.getByTestId("session-state")).toHaveTextContent("authed:a@b.com");
    expect(getUser).toHaveBeenCalledTimes(1);

    // Simulates logout()'s server-side redirect("/login") — SiteHeader lives
    // in the shared root layout and never remounts, but usePathname() DOES
    // change, which is the only signal available to re-verify.
    usePathname.mockReturnValue("/login");
    rerender(<Probe />);

    await waitFor(() => expect(getUser).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(screen.getByTestId("session-state")).toHaveTextContent("guest"),
    );
  });

  it("does not re-verify on a rerender with the same pathname", async () => {
    getUser.mockResolvedValue({ data: { user: { email: "a@b.com" } } });
    const { rerender } = render(<Probe />);
    await screen.findByTestId("session-state");
    expect(getUser).toHaveBeenCalledTimes(1);

    rerender(<Probe />);

    // No pathname change -> no new verify() -> no second network call.
    expect(getUser).toHaveBeenCalledTimes(1);
  });

  it("falls back to guest after a timeout + one retry when getUser() hangs (slow auth.portfonia.com proxy)", async () => {
    vi.useFakeTimers();
    getUser.mockReturnValue(new Promise(() => {})); // hangs forever
    render(<Probe />);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(8_000); // first attempt times out
    });
    expect(getUser).toHaveBeenCalledTimes(2); // timeout triggers one retry

    await act(async () => {
      await vi.advanceTimersByTimeAsync(8_000); // retry also times out
    });

    expect(screen.getByTestId("session-state")).toHaveTextContent("guest");
  });

  it("does not time out a getUser() call that resolves before the deadline", async () => {
    vi.useFakeTimers();
    getUser.mockResolvedValue({ data: { user: { email: "a@b.com" } } });
    render(<Probe />);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });

    expect(screen.getByTestId("session-state")).toHaveTextContent("authed:a@b.com");
    expect(getUser).toHaveBeenCalledTimes(1); // no retry needed
  });

  it("does not retry an immediate network error — only a timeout warrants a retry", async () => {
    getUser.mockRejectedValue(new Error("ECONNRESET"));
    render(<Probe />);

    await waitFor(() =>
      expect(screen.getByTestId("session-state")).toHaveTextContent("guest"),
    );
    // A dead endpoint retried with zero backoff just fails again — retrying
    // only helps the timeout case (a genuinely slow, not dead, round-trip).
    expect(getUser).toHaveBeenCalledTimes(1);
  });

  it("renders a login-pending state when markPendingLogin() was called before this mount", async () => {
    // Simulates the LoginForm's onSubmit firing right before the Server
    // Action's redirect() lands on the next page — the component that
    // called markPendingLogin() is gone by the time this mounts, so the
    // signal has to survive as module state across that navigation.
    markPendingLogin();
    getUser.mockReturnValue(new Promise(() => {})); // still verifying
    render(<Probe />);

    expect(await screen.findByTestId("session-state")).toHaveTextContent(
      "checking:login",
    );
  });

  it("clears the login-pending state once verification resolves", async () => {
    markPendingLogin();
    getUser.mockResolvedValue({ data: { user: { email: "a@b.com" } } });
    render(<Probe />);

    await waitFor(() =>
      expect(screen.getByTestId("session-state")).toHaveTextContent(
        "authed:a@b.com",
      ),
    );
  });

  it("does not show a login-pending state on an ordinary mount (no prior markPendingLogin())", () => {
    getUser.mockReturnValue(new Promise(() => {}));
    const { container } = render(<Probe />);

    expect(container).toBeEmptyDOMElement();
  });

  it("does not show a login-pending state after clearPendingLogin() (failed login must not leak into the next navigation)", () => {
    markPendingLogin();
    clearPendingLogin();
    getUser.mockReturnValue(new Promise(() => {}));
    const { container } = render(<Probe />);

    expect(container).toBeEmptyDOMElement();
  });

  it("optimistically flips to guest the instant markOptimisticLogout() is called, without waiting on getUser()", async () => {
    getUser.mockResolvedValue({ data: { user: { email: "a@b.com" } } });
    render(<Probe />);
    await waitFor(() =>
      expect(screen.getByTestId("session-state")).toHaveTextContent(
        "authed:a@b.com",
      ),
    );

    // getUser() never resolves again after this — the guest state must come
    // from the optimistic call itself, not from a network round-trip.
    getUser.mockReturnValue(new Promise(() => {}));
    act(() => {
      markOptimisticLogout();
    });

    expect(screen.getByTestId("session-state")).toHaveTextContent("guest");
  });

  it("discards a stale in-flight verify that resolves after an optimistic logout", async () => {
    let release!: (value: unknown) => void;
    getUser.mockReturnValue(
      new Promise((resolve) => {
        release = resolve;
      }),
    );
    render(<Probe />);

    act(() => {
      markOptimisticLogout();
    });
    expect(await screen.findByTestId("session-state")).toHaveTextContent("guest");

    act(() => {
      release({ data: { user: { email: "stale@b.com" } } });
    });

    expect(screen.getByTestId("session-state")).toHaveTextContent("guest");
  });

  it("revalidateSession after an optimistic logout re-runs getUser and can restore authed", async () => {
    getUser.mockResolvedValue({ data: { user: { email: "a@b.com" } } });
    render(<Probe />);
    await waitFor(() =>
      expect(screen.getByTestId("session-state")).toHaveTextContent(
        "authed:a@b.com",
      ),
    );

    act(() => {
      markOptimisticLogout();
    });
    expect(screen.getByTestId("session-state")).toHaveTextContent("guest");

    act(() => {
      revalidateSession();
    });

    await waitFor(() =>
      expect(screen.getByTestId("session-state")).toHaveTextContent(
        "authed:a@b.com",
      ),
    );
  });

  it("cleans up the subscription and listeners on unmount", async () => {
    getUser.mockResolvedValue({ data: { user: null } });
    const removeEventListener = vi.spyOn(document, "removeEventListener");
    const removeWindowListener = vi.spyOn(window, "removeEventListener");
    const { unmount } = render(<Probe />);
    await screen.findByTestId("session-state");

    unmount();

    expect(unsubscribe).toHaveBeenCalled();
    expect(removeEventListener).toHaveBeenCalledWith(
      "visibilitychange",
      expect.any(Function),
    );
    expect(removeWindowListener).toHaveBeenCalledWith(
      "focus",
      expect.any(Function),
    );
  });
});
