import { expect, it } from "vitest";
import { catalogs } from "./index";
const keys = ["deleteAccountDisclosure", "deleteAccountSubscription", "deleteAccountTokens", "deleteAccountGift", "deleteAccountReregister", "deleteAccountEmailLabel", "deleteAccountContinue", "deleteAccountPurchased", "deleteAccountRefundable", "deleteAccountVoluntary", "deleteAccountSignatureLabel", "deleteAccountRelinquish", "deleteAccountBalanceChanged", "deleteAccountInvalidCaptcha", "deleteAccountFailed", "deleteAccountIncomplete"];
it("15 every locale includes deletion strings with matching placeholders and cleared copy", () => {
  for (const catalog of Object.values(catalogs)) {
    const profile: Record<string, unknown> = catalog.profile;
    for (const key of keys) {
      expect(profile).toHaveProperty(key);
      const value = profile[key];
      expect(typeof value).toBe("string");
      if (typeof value === "string") {
        const english: Record<string, unknown> = catalogs.en.profile;
        expect(value.match(/\{\w+\}/g)).toEqual(String(english[key]).match(/\{\w+\}/g));
      }
    }
    expect(catalog.profile.deleteAccountBody).not.toMatch(/permanently|永久/);
    expect(profile).not.toHaveProperty("deleteAccountPlaceholder");
  }
});
it("17 privacy policy discloses fingerprint, tokens, waitlist and Profile deletion in every locale", () => {
  const expressions = [/one-way fingerprint/, /单向指纹/, /單向指紋/];
  const waitlists = [/waitlist entry/, /候补名单/, /候補名單/];
  const profiles = [/Profile page/, /\u4e2a\u4eba\u4e2d\u5fc3\u9875\u9762/, /\u500b\u4eba\u4e2d\u5fc3\u9801\u9762/];
  Object.values(catalogs).forEach((catalog, i) => {
    expect(catalog.legal.privacy.sections[5].body[1]).toMatch(expressions[i]);
    expect(catalog.legal.privacy.sections[5].body[1]).toContain("API token");
    expect(catalog.legal.privacy.sections[5].body[1]).toMatch(waitlists[i]);
    expect(catalog.legal.privacy.sections[7].body[0]).toMatch(profiles[i]);
  });
});
