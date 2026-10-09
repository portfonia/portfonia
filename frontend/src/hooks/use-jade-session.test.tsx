import { render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
const { fetchMock } = vi.hoisted(() => ({ fetchMock: vi.fn() }));
vi.mock("next/navigation", () => ({ usePathname: () => "/jade" }));
vi.mock("@/lib/supabase/browser", () => ({ createClient: () => ({ auth: { getUser: async () => ({ data: { user: { email: "jade@example.com" } } }), onAuthStateChange: () => ({ data: { subscription: { unsubscribe: vi.fn() } } }) } }) }));
import { useSession } from "./use-session";
function Flags() {
  const state = useSession();
  return <p>{state.status === "authed" ? `${state.advanced}:${state.jade}` : state.status}</p>;
}
beforeEach(() => { vi.stubGlobal("fetch", fetchMock); });
it.each([[true, true, "true:true"], [true, false, "true:false"], [false, undefined, "false:false"]])("session exposes advanced=%s jade=%s", async (advanced, jade, expected) => {
  fetchMock.mockResolvedValue({ ok: true, json: async () => ({ advanced, jade }) });
  render(<Flags />);
  expect(await screen.findByText(String(expected))).toBeInTheDocument();
});
it("session fails closed when backend probe fails", async () => {
  fetchMock.mockRejectedValue(new Error("unavailable"));
  render(<Flags />);
  expect(await screen.findByText("guest")).toBeInTheDocument();
});
