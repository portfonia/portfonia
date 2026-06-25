// Ring 0 lightweight i18n: all in-product strings live here instead of being
// scattered as hardcoded literals, so the Ring 2 migration to next-intl (see
// concept design doc section 10, frontend constraint 3) is a mechanical move.
// English only for now; add a `zh` map with the same shape when needed.

export const messages = {
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
    // Per-broker cross-check summary
    summaryHeading: "Cross-check by institution",
    summaryHint:
      "Cost basis is from your file (shares x avg cost, or supplied value), " +
      "not a live valuation. Use it to confirm every holding landed under the " +
      "right institution.",
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
    colIssues: "Notes",
    colRaw: "Original text",
    colReason: "Reason",

    // Errors
    errorUploadFailed: "Upload failed",
    errorSaveFailed: "Save failed",
    errorLoadFailed: "Could not load holdings",
  },
} as const;

export type Messages = typeof messages;
