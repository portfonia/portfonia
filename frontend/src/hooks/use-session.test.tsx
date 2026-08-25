import { render, screen, waitFor, act } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";

const { getUser, onAuthStateChange, unsubscribe } = vi.hoisted(() => ({
  getUser: vi.fn(),
  onAuthStateChange: vi.fn(),
  unsubscribe: vi.fn(),
}));

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

import { useSession } from "./use-session";

// Renders the hook state so each assertion can see exactly what a consumer
// (the Get Started menu) would render for the current session truth.
function Probe() {
  const session = useSession();
  if (session.status === "checking") return null;
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
    vi.clearAllMocks();
    vi.spyOn(console, "warn").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.restoreAllMocks();
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

  it("refreshes the identity on USER_UPDATED", async () => {
    getUser.mockResolvedValue({ data: { user: { email: "old@b.com" } } });
    render(<Probe />);
    await screen.findByTestId("session-state");

    act(() => {
      lastAuthCallback()("USER_UPDATED", {
        user: { email: "new@b.com" },
      });
    });

    await waitFor(() =>
      expect(screen.getByTestId("session-state")).toHaveTextContent(
        "authed:new@b.com",
      ),
    );
  });

  it("re-verifies when the tab becomes visible or focused again", async () => {
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

    await waitFor(() => expect(getUser).toHaveBeenCalledTimes(3));
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
