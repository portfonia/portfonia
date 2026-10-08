import { renderToString } from "react-dom/server";
import { expect, it, vi } from "vitest";
vi.mock("./waitlist-form", () => ({ WaitlistForm: () => null }));
vi.mock("./waitlist-heading", () => ({ WaitlistHeading: () => null }));
import WaitlistPage from "./page";
import { LocaleProvider } from "../_components/locale-provider";
it("server-renders language choices at the bottom of the waitlist page", () => {
  window.history.replaceState(null, "", "/zh-Hans/waitlist");
  const html = renderToString(<LocaleProvider routeLocale="zh-Hans"><WaitlistPage /></LocaleProvider>);
  expect(html).toContain('href="/zh-Hant/waitlist"');
  window.history.replaceState(null, "", "/");
});
