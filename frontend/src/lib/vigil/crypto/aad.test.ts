import { describe, expect, it } from "vitest";

import { buildFileAad, buildInnerAad } from "./aad";

const VAULT_ID = "11111111-1111-4111-8111-111111111111";
const OBJECT_ID = "22222222-2222-4222-8222-222222222222";

describe("buildFileAad / buildInnerAad", () => {
  it("produces the exact compact JSON array UTF-8 bytes from Appendix B", () => {
    const decoder = new TextDecoder();
    expect(decoder.decode(buildFileAad(VAULT_ID, OBJECT_ID))).toBe(
      `["vigil-file",1,"${VAULT_ID}","${OBJECT_ID}"]`,
    );
    expect(decoder.decode(buildInnerAad(VAULT_ID, OBJECT_ID))).toBe(
      `["vigil-inner",1,"${VAULT_ID}","${OBJECT_ID}"]`,
    );
  });

  it("has no whitespace (compact JSON)", () => {
    const decoder = new TextDecoder();
    expect(decoder.decode(buildFileAad(VAULT_ID, OBJECT_ID))).not.toMatch(/\s/);
    expect(decoder.decode(buildInnerAad(VAULT_ID, OBJECT_ID))).not.toMatch(/\s/);
  });

  it("differs between file and inner AAD for the same IDs (purpose separation)", () => {
    expect(buildFileAad(VAULT_ID, OBJECT_ID)).not.toEqual(buildInnerAad(VAULT_ID, OBJECT_ID));
  });

  it("differs when vault_id or object_id differ (binds AAD to the exact row)", () => {
    const other = "33333333-3333-4333-8333-333333333333";
    expect(buildFileAad(VAULT_ID, OBJECT_ID)).not.toEqual(buildFileAad(other, OBJECT_ID));
    expect(buildFileAad(VAULT_ID, OBJECT_ID)).not.toEqual(buildFileAad(VAULT_ID, other));
  });
});
