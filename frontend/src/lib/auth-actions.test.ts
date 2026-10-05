import { expect, it, vi } from "vitest";
const { signOut, redirect } = vi.hoisted(() => ({ signOut: vi.fn(), redirect: vi.fn() }));
vi.mock("@/lib/supabase/server", () => ({ createClient: async () => ({ auth: { signOut } }) }));
vi.mock("next/navigation", () => ({ redirect }));
import * as actions from "./auth-actions";
it("12 account deletion clears only the local session and redirects home", async () => {
  expect(actions).toHaveProperty("logoutAfterAccountDeletion");
  const action = Reflect.get(actions, "logoutAfterAccountDeletion") as () => Promise<void>;
  await action();
  expect(signOut).toHaveBeenCalledExactlyOnceWith({ scope: "local" });
  expect(redirect).toHaveBeenCalledExactlyOnceWith("/");
});
