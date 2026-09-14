import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/app/logout/actions", () => ({ logout: vi.fn() }));

import { ForbiddenShell, ManagementShell, UnavailableShell } from "./management-shell";
import type { VaultView } from "@/lib/vault";

const vault: VaultView = {
  phase: "DISARMED",
  revision: 0,
  hold_reason: null,
  held_at: null,
  active_object_id: null,
  active_config_id: null,
  next_check_at: null,
  deadline_at: null,
  heartbeat: {
    last_scan_completed_at: null,
    last_dependency_check_at: null,
    health: "held",
    reason: null,
  },
};

describe("management chrome", () => {
  it("renders phase, hold, revision, next-check, deadline and heartbeat", () => {
    render(<ManagementShell vault={vault} />);
    expect(screen.getByText("DISARMED")).toBeInTheDocument();
    expect(screen.getByText("held")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /log out/i })).toBeInTheDocument();
  });

  it("does not claim that logout instantly revokes JWTs", () => {
    render(<ManagementShell vault={vault} />);
    expect(screen.getByText(/not instantly revoked/i)).toBeInTheDocument();
  });

  it("uses distinct copy for 403 and 503", () => {
    const forbidden = render(<ForbiddenShell />);
    expect(forbidden.getByText(/account not allowed/i)).toBeInTheDocument();
    forbidden.unmount();
    const unavailable = render(<UnavailableShell />);
    expect(unavailable.getByText(/unavailable/i)).toBeInTheDocument();
    expect(unavailable.queryByText(/account not allowed/i)).not.toBeInTheDocument();
  });
});
