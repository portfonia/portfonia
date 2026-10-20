import { renderHook } from "@testing-library/react";
import { act } from "react";
import { cleanup } from "@testing-library/react";
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

import { useIdleLogout, SESSION_IDLE_TIMEOUT_MS } from "./use-idle-logout";

let testDay = 0;
const IDLE_MS = SESSION_IDLE_TIMEOUT_MS;

function armAuthed() {
  session.current = { status: "authed", email: "a@b.com" };
}

describe("useIdleLogout", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-10-20T00:00:00Z").getTime() + testDay++ * 86_400_000);
    const storage = new Map<string, string>();
    vi.stubGlobal("localStorage", { getItem: (key: string) => storage.get(key) ?? null, setItem: (key: string, value: string) => { storage.set(key, value); } });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ status: 200 }));
    Object.defineProperty(document, "hidden", { configurable: true, value: false });
    session.current = { status: "guest" };
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("calls the idle callback exactly once with the expired reason after the timeout elapses", async () => {
    armAuthed();
    renderHook(() => useIdleLogout("authed", logout));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(IDLE_MS + 30_000);
    });

    expect(logout).toHaveBeenCalledTimes(1);
    expect(logout).toHaveBeenCalledWith("expired");
  });

  it("resets the clock on user activity signals, so continuous activity never logs out", async () => {
    armAuthed();
    renderHook(() => useIdleLogout("authed", logout));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10 * 60 * 1000);
      window.dispatchEvent(new Event("keydown"));
      await vi.advanceTimersByTimeAsync(10 * 60 * 1000);
      window.dispatchEvent(new Event("pointerdown"));
      await vi.advanceTimersByTimeAsync(10 * 60 * 1000); // 30 min total, gaps < 15
    });

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

  it("809 A7 expires exactly once at fifteen minutes in visible and hidden tabs", async () => {
    for (const hidden of [false, true]) {
      Object.defineProperty(document, "hidden", { configurable: true, value: hidden });
      logout.mockClear();
      const { unmount } = renderHook(() => useIdleLogout("authed", logout));
      await act(async () => { await vi.advanceTimersByTimeAsync(IDLE_MS - 1); });
      expect(logout).not.toHaveBeenCalled();
      await act(async () => { await vi.advanceTimersByTimeAsync(1); });
      expect(logout).toHaveBeenCalledExactlyOnceWith("expired");
      await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
      expect(logout).toHaveBeenCalledTimes(1);
      unmount();
    }
  });

  it("809 A8 throttles heartbeat attempts including failures, expires on 401 and ignores rejection", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockRejectedValueOnce(new Error("offline"));
    renderHook(() => useIdleLogout("authed", logout));
    window.dispatchEvent(new Event("scroll"));
    expect(fetchMock).not.toHaveBeenCalled();
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); window.dispatchEvent(new Event("scroll")); });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(logout).not.toHaveBeenCalled();
    await act(async () => { window.dispatchEvent(new Event("keydown")); await vi.advanceTimersByTimeAsync(59_999); window.dispatchEvent(new Event("wheel")); });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    let resolve!: (response: Response) => void;
    fetchMock.mockReturnValueOnce(new Promise<Response>(done => { resolve = done; }));
    await act(async () => { await vi.advanceTimersByTimeAsync(1); window.dispatchEvent(new Event("pointerdown")); });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); window.dispatchEvent(new Event("scroll")); });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await act(async () => { resolve({ status: 200 } as Response); });
    fetchMock.mockResolvedValueOnce({ status: 401 } as Response);
    await act(async () => { window.dispatchEvent(new Event("touchstart")); });
    expect(logout).toHaveBeenCalledExactlyOnceWith("expired");
  });

  it("809 A9 postpones expiry using a newer backend touch from another tab", async () => {
    renderHook(() => useIdleLogout("authed", logout));
    await act(async () => { await vi.advanceTimersByTimeAsync(10 * 60_000); });
    localStorage.setItem("portfonia.lastBackendTouch", String(Date.now()));
    await act(async () => { await vi.advanceTimersByTimeAsync(5 * 60_000); });
    expect(logout).not.toHaveBeenCalled();
    await act(async () => { await vi.advanceTimersByTimeAsync(10 * 60_000); });
    expect(logout).toHaveBeenCalledExactlyOnceWith("expired");
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
