export type VaultHeartbeat = {
  last_scan_completed_at: string | null;
  last_dependency_check_at: string | null;
  health: string;
  reason: string | null;
};

export type VaultView = {
  phase: string;
  revision: number;
  hold_reason: string | null;
  held_at: string | null;
  active_object_id: string | null;
  active_config_id: string | null;
  next_check_at: string | null;
  deadline_at: string | null;
  heartbeat: VaultHeartbeat;
};

export type VaultLoad =
  | { status: "ok"; vault: VaultView }
  | { status: "unauthenticated" }
  | { status: "forbidden" }
  | { status: "unavailable" };
