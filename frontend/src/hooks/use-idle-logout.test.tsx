import { renderHook } from "@testing-library/react";
import { act } from "react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";

const { session, logout } = vi.hoisted(() => ({
  // Mutable holder so individual tests control what useSession reports.
  // Kept as a stable object reference between renders — mirroring how the
  // real useState-backed useSession behaves when nothing changed.
  session: { current: { status: "guest" } as Record<string, unknown> },
  logout: vi.fn(),
}));

vi.mock("@/hooks/use-session", () => ({ useSession: () => session.current }));
vi.mock("@/lib/auth-actions", () => ({ logout }));

import { useIdleLogout } from "./use-idle-logout";

const IDLE_MS = 15 * 60 * 1000;

function armAuthed() {
  session.current = { status: "authed", email: "a@b.com" };
}

describe("useIdleLogout", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers();
    session.current = { status: "guest" };
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("calls the idle callback exactly once after the idle timeout elapses", async () => {
    armAuthed();
    renderHook(() => useIdleLogout("authed", logout));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(IDLE_MS + 30_000);
    });

    expect(logout).toHaveBeenCalledTimes(1);
  });

  it("resets the clock on user activity signals, so continuous activity never logs out", async () => {
    armAuthed();
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderHook(() => useIdleLogout("authed", logout));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10 * 60 * 1000);
      window.dispatchEvent(new Event("keydown"));
      await vi.advanceTimersByTimeAsync(10 * 60 * 1000);
      window.dispatchEvent(new Event("pointerdown"));
      await vi.advanceTimersByTimeAsync(10 * 60 * 1000); // 30 min total, gaps < 15
    });
    void user;

    expect(logout).not.toHaveBeenCalled();
  });

  it("still fires when a single idle gap exceeds the timeout, despite earlier activity", async () => {
    armAuthed();
    renderHook(() => useIdleLogout("authed", logout));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(14 * 60 * 1000);
      window.dispatchEvent(new Event("scroll"));
      await vi.advanceTimersByTimeAsync(IDLE_MS + 30_000);
    });

    expect(logout).toHaveBeenCalledTimes(1);
  });

  it("does not arm for guests", async () => {
    session.current = { status: "guest" };
    renderHook(() => useIdleLogout("guest", logout));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60 * 60 * 1000);
    });

    expect(logout).not.toHaveBeenCalled();
  });

  it("arms when the session flips from guest to authed without remounting", async () => {
    session.current = { status: "guest" };
    const { rerender } = renderHook(
      ({ status }) => useIdleLogout(status, logout),
      { initialProps: { status: "guest" as "guest" | "authed" } },
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5 * 60 * 1000);
    });
    rerender({ status: "authed" });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(IDLE_MS + 30_000);
    });

    expect(logout).toHaveBeenCalledTimes(1);
  });

  it("disarms on unmount", async () => {
    armAuthed();
    const { unmount } = renderHook(() => useIdleLogout("authed", logout));
    unmount();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(IDLE_MS + 30_000);
    });

    expect(logout).not.toHaveBeenCalled();
  });
});
