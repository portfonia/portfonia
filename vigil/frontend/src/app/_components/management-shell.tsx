"use client";

import { logout } from "@/app/logout/actions";
import type { VaultView } from "@/lib/vault";

function display(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  return String(value);
}

export function ManagementShell({ vault }: { vault: VaultView }) {
  return (
    <main className="mx-auto flex max-w-2xl flex-col gap-6 px-6 py-16">
      <header className="flex items-center justify-between gap-4">
        <h1 className="text-3xl">Vigil</h1>
        <form action={logout}>
          <button type="submit" className="rounded-md border border-zinc-500 px-3 py-1 text-sm">
            Log out
          </button>
        </form>
      </header>
      <p className="text-sm opacity-70">
        Signing out clears this site&apos;s session cookie and the shared Auth session on this
        browser. Already-issued access tokens are not instantly revoked.
      </p>
      <dl className="grid grid-cols-[12rem_1fr] gap-x-4 gap-y-2 text-sm">
        <dt className="opacity-70">Phase</dt>
        <dd>{vault.phase}</dd>
        <dt className="opacity-70">Hold reason</dt>
        <dd>{display(vault.hold_reason)}</dd>
        <dt className="opacity-70">Revision</dt>
        <dd>{vault.revision}</dd>
        <dt className="opacity-70">Next check</dt>
        <dd>{display(vault.next_check_at)}</dd>
        <dt className="opacity-70">Deadline</dt>
        <dd>{display(vault.deadline_at)}</dd>
        <dt className="opacity-70">Active object</dt>
        <dd>{display(vault.active_object_id)}</dd>
        <dt className="opacity-70">Active config</dt>
        <dd>{display(vault.active_config_id)}</dd>
        <dt className="opacity-70">Heartbeat health</dt>
        <dd>{vault.heartbeat.health}</dd>
        <dt className="opacity-70">Heartbeat reason</dt>
        <dd>{display(vault.heartbeat.reason)}</dd>
        <dt className="opacity-70">Last scan</dt>
        <dd>{display(vault.heartbeat.last_scan_completed_at)}</dd>
        <dt className="opacity-70">Last dependency check</dt>
        <dd>{display(vault.heartbeat.last_dependency_check_at)}</dd>
      </dl>
    </main>
  );
}

export function ForbiddenShell() {
  return (
    <main className="mx-auto max-w-lg px-6 py-24">
      <h1 className="text-3xl">Account not allowed</h1>
      <p className="mt-4 text-sm opacity-80">
        This Portfonia account is not the Vigil owner. Management is hidden.
      </p>
    </main>
  );
}

export function UnavailableShell() {
  return (
    <main className="mx-auto max-w-lg px-6 py-24">
      <h1 className="text-3xl">Vigil is unavailable</h1>
      <p className="mt-4 text-sm opacity-80">
        A Vigil dependency failed. This is not an account-permission error. Try again later.
      </p>
    </main>
  );
}
