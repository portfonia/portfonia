import { expect, it, vi } from "vitest";
vi.mock("./login/login-form", () => ({ LoginForm: () => null }));
vi.mock("./signup/signup-form", () => ({ SignupForm: () => null }));
vi.mock("./forgot-password/forgot-password-form", () => ({ ForgotPasswordForm: () => null }));
vi.mock("./reset-password/reset-password-form", () => ({ ResetPasswordForm: () => null }));
vi.mock("./verify-email/verify-email-form", () => ({ VerifyEmailForm: () => null }));
vi.mock("./unsubscribe/unsubscribe-form", () => ({ UnsubscribeForm: () => null }));
import * as login from "./login/page";
import * as signup from "./signup/page";
import * as forgot from "./forgot-password/page";
import * as reset from "./reset-password/page";
import * as verify from "./verify-email/page";
import * as unsubscribe from "./unsubscribe/page";
import sitemap from "./sitemap";
import robots from "./robots";
import { NOINDEX_METADATA } from "@/lib/seo";

it.each([["login", login], ["signup", signup], ["forgot-password", forgot], ["reset-password", reset], ["verify-email", verify], ["unsubscribe", unsubscribe]] as const)("7e marks %s noindex/follow and leaves it crawlable but outside the sitemap", (path, page) => {
  expect(page).toHaveProperty("metadata.robots", { index: false, follow: true });
  expect(page.metadata).toEqual(NOINDEX_METADATA);
  expect(page.metadata).not.toHaveProperty("alternates");
  const rules = robots().rules;
  expect(Array.isArray(rules) && rules[0].disallow).not.toContain(`/${path}`);
  expect(sitemap().some((entry) => new URL(entry.url).pathname === `/${path}`)).toBe(false);
});
