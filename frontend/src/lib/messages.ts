// Ring 0 lightweight i18n: all in-product strings live here instead of being
// scattered as hardcoded literals, so the Ring 2 migration to next-intl (see
// concept design doc section 10, frontend constraint 3) is a mechanical move.
// English only for now; add a `zh` map with the same shape when needed.

import { SESSION_IDLE_TIMEOUT_MS } from "./idle-timeout";

const idleMinutes = Math.round(SESSION_IDLE_TIMEOUT_MS / 60_000);

export const messages = {
  common: {
    brandName: "Portfonia",
  },
  menu: {
    // Top-bar Get Started menu (issue #207). English-only for now, like the
    // rest of this map — the home route overrides labels via
    // home-messages.nav until a zh map lands here.
    trigger: "Get Started",
    login: "Log in",
    logout: "Log out",
    holdings: "Holdings",
    questionnaire: "Investment style",
    // Composed from SESSION_IDLE_TIMEOUT_MS (lib/idle-timeout.ts) so the
    // message cannot drift from enforcement.
    sessionExpired: `Your session ended after ${idleMinutes} minutes of inactivity.`,
  },
  auth: {
    loginHeading: "Log in",
    loginSubtitle: "Welcome back.",
    signupHeading: "Create your account",
    signupSubtitle: "You'll need a valid invite link to sign up.",
    emailLabel: "Email",
    passwordLabel: "Password",
    loginButton: "Log in",
    loggingIn: "Logging in...",
    signupButton: "Create account",
    signingUp: "Creating account...",
    missingInvite:
      "This link is missing its invite token. Ask whoever invited you for a fresh link.",
    noAccountYet: "Need an invite? Ask the person who referred you.",
    alreadyHaveAccount: "Already have an account?",
    backToLogin: "Log in instead",
  },
  holdings: {
    pageTitle: "Holdings",
    pageSubtitle: "Upload a file to import your holdings. Review, then save.",

    // Current holdings table
    currentHeading: "Current holdings",
    emptyState: "No holdings yet. Upload a file to get started.",
    exportButton: "Export current holdings",
    downloadTemplate: "Download template",

    // Upload
    uploadHeading: "Import from file",
    uploadHint:
      "Accepted: .md, .txt, .csv, .xlsx, .xls. One sheet per Excel file. " +
      "Add .HK for Hong Kong tickers, .SS / .SZ for A-shares; US tickers need " +
      "no suffix. Chinese public funds: enter the 6-digit fund code.",
    chooseFile: "Choose file",
    uploading: "Parsing...",
    parseAgain: "Choose a different file",
    uploadingProgress: (seconds: number) => {
      if (seconds < 5) return "Reading file...";
      if (seconds < 20) return "Parsing with AI...";
      if (seconds < 45) return `Still working (${seconds}s)...`;
      return `Taking longer than usual (${seconds}s) — the LLM provider may be slow.`;
    },

    // Preview
    previewHeading: "Parsed holdings",
    previewValidCount: (n: number) =>
      `${n} row${n === 1 ? "" : "s"} ready to save`,
    inferredNote:
      "System inferred some fields — please review highlighted rows.",
    // Per-row notes / resolutions (potential conflicts + how they were handled)
    rowNotesLabel: "Potential issues / how they were handled:",
    // Per-broker cross-check summary
    summaryHeading: "Cross-check by institution",
    summaryHint:
      "Computed from your uploaded file only (shares x avg cost, or the value " +
      "you supplied) — NO current market prices are fetched. This is a " +
      "cost-basis cross-check to confirm every holding landed under the right " +
      "institution, not a live valuation.",
    summaryColBroker: "Institution",
    summaryColCount: "Holdings",
    summaryColCostBasis: "Cost basis",
    issuesHeading: "Could not be parsed",
    issuesCount: (n: number) =>
      `${n} row${n === 1 ? "" : "s"} will NOT be saved unless you fix them`,
    saveButton: "Save holdings",
    saving: "Saving...",
    cancelButton: "Cancel",

    // Last-chance confirm dialog
    confirmTitle: "Discard unparsed rows?",
    confirmBody: (n: number) =>
      `${n} row${n === 1 ? "" : "s"} could not be parsed and will be ` +
      `permanently discarded. This is your last chance to recover them. ` +
      `To keep them, download the file, fix the rows, and re-upload instead.`,
    confirmKeep: "Go back and fix",
    confirmDiscard: "Discard and save",

    // Full-replace warning
    replaceWarning: "Saving replaces ALL current holdings with the rows below.",

    // Table column headers
    colName: "Name",
    colTicker: "Ticker",
    colFundCode: "Fund code",
    colCurrency: "Currency",
    colShares: "Shares",
    colAvgCost: "Avg cost",
    colCurrentValue: "Current value",
    colPricingMode: "Pricing",
    colAssetType: "Type",
    colBroker: "Broker",
    colRaw: "Original text",
    colReason: "Reason",

    // Errors
    errorUploadFailed: "Upload failed",
    errorSaveFailed: "Save failed",
    errorLoadFailed: "Could not load holdings",
  },
  questionnaire: {
    pageTitle: "Investment style",
    pageSubtitle:
      "Helps tailor which facts and angles your reports emphasize. Every " +
      "question is pre-filled with a sensible default — skip straight to " +
      "Save if you don't want to change anything, or come back and " +
      "re-answer any time.",
    stepOf: (current: number, total: number) => `Question ${current} of ${total}`,
    back: "Back",
    next: "Next",
    skip: "Skip for now",
    save: "Save",
    saving: "Saving...",
    saved: "Saved.",
    errorSaveFailed: "Could not save your answers",
    errorLoadFailed: "Could not load your saved answers",
    freeTextHeading: "Anything else worth knowing?",
    freeTextHint:
      "Optional. No format required — write as much or as little as you like. " +
      "Stored and shown back to you exactly as written.",
    freeTextPlaceholder: "e.g. some positions are legacy holdings, not active choices...",

    dims: {
      asset_scale: {
        question: "What's your current investable asset scale?",
        options: {
          UNDER_100K: "Under $100K",
          "100K_500K": "$100K – $500K",
          "500K_2M": "$500K – $2M",
          OVER_2M: "Over $2M",
        },
      },
      markets: {
        question: "Which markets do you mainly invest in? (select all that apply)",
        options: { US: "US", HK: "Hong Kong", "A-Share": "A-Share", Other: "Other" },
      },
      style: {
        question: "How would you describe your investing style?",
        options: { VALUE: "Value", GROWTH: "Growth", INDEX: "Index", MIXED: "Mixed" },
      },
      horizon: {
        question: "What's your typical holding period?",
        options: {
          SHORT: "Short-term (under 1 year)",
          MEDIUM: "Medium-term (1–3 years)",
          LONG: "Long-term (3+ years)",
        },
      },
      risk_appetite: {
        question: "How would you describe your risk appetite?",
        options: {
          CONSERVATIVE: "Conservative",
          BALANCED: "Balanced",
          AGGRESSIVE: "Aggressive",
        },
      },
      sectors_of_interest: {
        question: "Any sectors you especially want covered? (select all that apply, or none)",
        options: {
          Technology: "Technology",
          Communication: "Communication",
          Financials: "Financials",
          Healthcare: "Healthcare",
          "Consumer Discretionary": "Consumer Discretionary",
          "Consumer Staples": "Consumer Staples",
          Energy: "Energy",
          Materials: "Materials",
          Industrials: "Industrials",
          "Real Estate": "Real Estate",
          Utilities: "Utilities",
          Other: "Other",
        },
      },
      objective: {
        question: "What's your core objective?",
        options: {
          PRESERVATION: "Capital preservation",
          GROWTH: "Growth",
          INCOME: "Income",
        },
      },
      intel_focus: {
        question: "Where should reports focus their intelligence?",
        options: {
          MACRO: "Macro signals",
          FUNDAMENTALS: "Individual-holding fundamentals",
          GEOPOLITICS: "Geopolitical developments",
          BALANCED: "Balanced across all of these",
        },
      },
    },
  },
} as const;

export type Messages = typeof messages;
