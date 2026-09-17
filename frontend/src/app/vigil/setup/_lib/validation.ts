// Pure, side-effect-free form validation for /vigil/setup (issue #455).
// Mirrors the backend's own bounds exactly (services/vigil/configuration.py,
// services/vigil/objects.py, #450 Design section 5 Appendix B) so the form
// fails fast with the same rules the server will enforce anyway — this is
// a UX convenience, not a substitute for the server's authoritative checks.

export const VIGIL_MAX_PLAINTEXT_BYTES = 10_000_000;
export const VIGIL_MIN_PASSWORD_BYTES = 1;
export const VIGIL_MAX_PASSWORD_BYTES = 1024;
export const VIGIL_INTERVAL_DAYS_OPTIONS = [7, 14, 30, 60, 90] as const;
export const VIGIL_INTERVAL_DAYS_DEFAULT = 30;
export const VIGIL_GRACE_HOURS_MIN = 24;
export const VIGIL_GRACE_HOURS_MAX = 168;
export const VIGIL_GRACE_HOURS_DEFAULT = 72;
export const VIGIL_MAX_RECIPIENTS = 3;
export const VIGIL_MIN_RECIPIENTS = 1;
export const VIGIL_MAX_MESSAGE_CODEPOINTS = 4000;

export interface SetupRecipientForm {
  email: string;
  email_confirm: string;
}

export interface SetupFormState {
  /** null = no file selected. */
  fileSize: number | null;
  /** null = no explicit choice made yet (must not silently default). */
  hasPassword: boolean | null;
  password: string;
  passwordConfirm: string;
  recipients: SetupRecipientForm[];
  message: string;
  intervalDays: number;
  graceHours: number;
}

export type SetupFormErrorCode =
  | "fileRequired"
  | "fileTooLarge"
  | "passwordChoiceRequired"
  | "passwordRequired"
  | "passwordMismatch"
  | "passwordTooLong"
  | "recipientsRequired"
  | "recipientsTooMany"
  | "recipientEmailMismatch"
  | "recipientEmailInvalid"
  | "recipientsDuplicate"
  | "intervalInvalid"
  | "graceInvalid"
  | "messageTooLong";

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function utf8ByteLength(value: string): number {
  return new TextEncoder().encode(value).length;
}

export function validateSetupForm(form: SetupFormState): SetupFormErrorCode[] {
  const errors: SetupFormErrorCode[] = [];

  if (form.fileSize === null) {
    errors.push("fileRequired");
  } else if (form.fileSize > VIGIL_MAX_PLAINTEXT_BYTES) {
    errors.push("fileTooLarge");
  }

  if (form.hasPassword === null) {
    errors.push("passwordChoiceRequired");
  } else if (form.hasPassword) {
    if (form.password.length === 0) {
      errors.push("passwordRequired");
    } else if (utf8ByteLength(form.password) > VIGIL_MAX_PASSWORD_BYTES) {
      errors.push("passwordTooLong");
    } else if (form.password !== form.passwordConfirm) {
      errors.push("passwordMismatch");
    }
  }

  if (form.recipients.length < VIGIL_MIN_RECIPIENTS) {
    errors.push("recipientsRequired");
  } else if (form.recipients.length > VIGIL_MAX_RECIPIENTS) {
    errors.push("recipientsTooMany");
  } else {
    const seen = new Set<string>();
    let hasMismatch = false;
    let hasInvalid = false;
    let hasDuplicate = false;
    for (const recipient of form.recipients) {
      if (recipient.email !== recipient.email_confirm) {
        hasMismatch = true;
        continue;
      }
      if (!EMAIL_RE.test(recipient.email.trim())) {
        hasInvalid = true;
        continue;
      }
      const key = recipient.email.trim().toLowerCase();
      if (seen.has(key)) hasDuplicate = true;
      seen.add(key);
    }
    if (hasMismatch) errors.push("recipientEmailMismatch");
    if (hasInvalid) errors.push("recipientEmailInvalid");
    if (hasDuplicate) errors.push("recipientsDuplicate");
  }

  if (!(VIGIL_INTERVAL_DAYS_OPTIONS as readonly number[]).includes(form.intervalDays)) {
    errors.push("intervalInvalid");
  }

  if (form.graceHours < VIGIL_GRACE_HOURS_MIN || form.graceHours > VIGIL_GRACE_HOURS_MAX) {
    errors.push("graceInvalid");
  }

  if ([...form.message].length > VIGIL_MAX_MESSAGE_CODEPOINTS) {
    errors.push("messageTooLong");
  }

  return errors;
}
