import { describe, expect, it } from "vitest";

import {
  VIGIL_GRACE_HOURS_MAX,
  VIGIL_GRACE_HOURS_MIN,
  VIGIL_INTERVAL_DAYS_OPTIONS,
  VIGIL_MAX_PLAINTEXT_BYTES,
  validateSetupForm,
  type SetupFormState,
} from "./validation";

function baseForm(overrides: Partial<SetupFormState> = {}): SetupFormState {
  return {
    fileSize: 1024,
    hasPassword: false,
    password: "",
    passwordConfirm: "",
    recipients: [{ email: "a@example.com", email_confirm: "a@example.com" }],
    message: "",
    intervalDays: 30,
    graceHours: 72,
    ...overrides,
  };
}

describe("validateSetupForm", () => {
  it("accepts a minimal valid no-password form", () => {
    expect(validateSetupForm(baseForm())).toEqual([]);
  });

  it("requires a file to be selected", () => {
    const errors = validateSetupForm({ ...baseForm(), fileSize: null });
    expect(errors).toContain("fileRequired");
  });

  it(`rejects a file over ${VIGIL_MAX_PLAINTEXT_BYTES} bytes`, () => {
    const errors = validateSetupForm(baseForm({ fileSize: VIGIL_MAX_PLAINTEXT_BYTES + 1 }));
    expect(errors).toContain("fileTooLarge");
  });

  it("accepts a file at exactly the max size", () => {
    expect(validateSetupForm(baseForm({ fileSize: VIGIL_MAX_PLAINTEXT_BYTES }))).toEqual([]);
  });

  it("accepts a zero-byte file", () => {
    expect(validateSetupForm(baseForm({ fileSize: 0 }))).toEqual([]);
  });

  it("requires an explicit password choice (not undecided)", () => {
    const errors = validateSetupForm(baseForm({ hasPassword: null }));
    expect(errors).toContain("passwordChoiceRequired");
  });

  it("requires a non-empty password when hasPassword is true", () => {
    const errors = validateSetupForm(baseForm({ hasPassword: true, password: "", passwordConfirm: "" }));
    expect(errors).toContain("passwordRequired");
  });

  it("requires password and passwordConfirm to match", () => {
    const errors = validateSetupForm(
      baseForm({ hasPassword: true, password: "correct-horse", passwordConfirm: "wrong" }),
    );
    expect(errors).toContain("passwordMismatch");
  });

  it("rejects a password over 1024 UTF-8 bytes", () => {
    const long = "x".repeat(1025);
    const errors = validateSetupForm(baseForm({ hasPassword: true, password: long, passwordConfirm: long }));
    expect(errors).toContain("passwordTooLong");
  });

  it("accepts a valid password", () => {
    const errors = validateSetupForm(
      baseForm({ hasPassword: true, password: "correct horse battery staple", passwordConfirm: "correct horse battery staple" }),
    );
    expect(errors).toEqual([]);
  });

  it("requires at least 1 recipient", () => {
    const errors = validateSetupForm(baseForm({ recipients: [] }));
    expect(errors).toContain("recipientsRequired");
  });

  it("rejects more than 3 recipients", () => {
    const errors = validateSetupForm(
      baseForm({
        recipients: [
          { email: "a@example.com", email_confirm: "a@example.com" },
          { email: "b@example.com", email_confirm: "b@example.com" },
          { email: "c@example.com", email_confirm: "c@example.com" },
          { email: "d@example.com", email_confirm: "d@example.com" },
        ],
      }),
    );
    expect(errors).toContain("recipientsTooMany");
  });

  it("rejects a recipient whose email/email_confirm don't match", () => {
    const errors = validateSetupForm(
      baseForm({ recipients: [{ email: "a@example.com", email_confirm: "b@example.com" }] }),
    );
    expect(errors).toContain("recipientEmailMismatch");
  });

  it("rejects a malformed recipient email", () => {
    const errors = validateSetupForm(
      baseForm({ recipients: [{ email: "not-an-email", email_confirm: "not-an-email" }] }),
    );
    expect(errors).toContain("recipientEmailInvalid");
  });

  it("rejects duplicate recipient emails", () => {
    const errors = validateSetupForm(
      baseForm({
        recipients: [
          { email: "a@example.com", email_confirm: "a@example.com" },
          { email: "A@Example.com", email_confirm: "A@Example.com" },
        ],
      }),
    );
    expect(errors).toContain("recipientsDuplicate");
  });

  it(`accepts every allowed interval (${VIGIL_INTERVAL_DAYS_OPTIONS.join(", ")})`, () => {
    for (const days of VIGIL_INTERVAL_DAYS_OPTIONS) {
      expect(validateSetupForm(baseForm({ intervalDays: days }))).toEqual([]);
    }
  });

  it("rejects an interval outside the allowed set", () => {
    const errors = validateSetupForm(baseForm({ intervalDays: 45 }));
    expect(errors).toContain("intervalInvalid");
  });

  it(`accepts grace hours at the ${VIGIL_GRACE_HOURS_MIN}/${VIGIL_GRACE_HOURS_MAX} boundaries`, () => {
    expect(validateSetupForm(baseForm({ graceHours: VIGIL_GRACE_HOURS_MIN }))).toEqual([]);
    expect(validateSetupForm(baseForm({ graceHours: VIGIL_GRACE_HOURS_MAX }))).toEqual([]);
  });

  it("rejects grace hours outside the bounds", () => {
    expect(validateSetupForm(baseForm({ graceHours: VIGIL_GRACE_HOURS_MIN - 1 }))).toContain("graceInvalid");
    expect(validateSetupForm(baseForm({ graceHours: VIGIL_GRACE_HOURS_MAX + 1 }))).toContain("graceInvalid");
  });

  it("rejects a message over 4000 codepoints", () => {
    const errors = validateSetupForm(baseForm({ message: "x".repeat(4001) }));
    expect(errors).toContain("messageTooLong");
  });

  it("accepts a message at exactly 4000 codepoints", () => {
    expect(validateSetupForm(baseForm({ message: "x".repeat(4000) }))).toEqual([]);
  });
});
