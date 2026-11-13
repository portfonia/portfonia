import { render } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AppShell } from "./app-shell";

const { useLocale, useSession, usePathname, recordPageView } = vi.hoisted(() => ({
  useLocale: vi.fn(), useSession: vi.fn(), usePathname: vi.fn(), recordPageView: vi.fn(),
}));

vi.mock("./locale-provider", () => ({ useLocale }));
vi.mock("@/hooks/use-session", () => ({ useSession }));
vi.mock("next/navigation", () => ({ usePathname }));
vi.mock("@/lib/api", () => ({ recordPageView }));

beforeEach(() => {
  vi.clearAllMocks();
  useLocale.mockReturnValue({ locale: "en" });
  useSession.mockReturnValue({ status: "guest" });
  usePathname.mockReturnValue("/profile");
  recordPageView.mockResolvedValue(undefined);
});

describe("AppShell", () => {
  it("records each authed pathname change and no guest page views", () => {
    const { rerender } = render(<AppShell>content</AppShell>);
    expect(recordPageView).not.toHaveBeenCalled();
    useSession.mockReturnValue({ status: "authed" });
    rerender(<AppShell>content</AppShell>);
    expect(recordPageView).toHaveBeenCalledExactlyOnceWith("/profile");
    usePathname.mockReturnValue("/holdings");
    rerender(<AppShell>content</AppShell>);
    usePathname.mockReturnValue("/profile");
    rerender(<AppShell>content</AppShell>);
    expect(recordPageView.mock.calls).toEqual([["/profile"], ["/holdings"], ["/profile"]]);
    useSession.mockReturnValue({ status: "guest" });
    usePathname.mockReturnValue("/");
    rerender(<AppShell>content</AppShell>);
    expect(recordPageView).toHaveBeenCalledTimes(3);
  });
  it.each(["en", "zh-Hans", "zh-Hant"] as const)(
    "sets lang=%s to match the selected locale on every route (issue #209: home-only gate removed)",
    (locale) => {
      useLocale.mockReturnValue({ locale, setLocale: vi.fn() });

      const { container } = render(
        <AppShell>
          <p>content</p>
        </AppShell>,
      );

      expect(container.firstElementChild).toHaveAttribute("lang", locale);
    },
  );
});
