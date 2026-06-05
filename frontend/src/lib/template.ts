// Client-side holdings template. Ring 0 has no dedicated backend template
// endpoint; the empty template (with example rows + rules) is generated here.
// Format matches what the LLM parser and GET /holdings/export produce.

export const TEMPLATE_MARKDOWN = `##### Holdings template
#####
##### One holding per line. Order is flexible — the parser reads free-form text.
##### Columns suggested: name  ticker/fund-code  currency  shares  avg-cost  broker
##### For cash / bank products with no public code: name  total-value  broker
#####
##### Ticker suffixes: .HK (Hong Kong), .SS / .SZ (A-shares), US tickers need none.
##### Chinese public funds: enter the 6-digit fund code (e.g. 110011).
##### Lines starting with ##### are comments and will be ignored by the parser.
#####
##### --- examples (delete these lines and add your own) ---
##### Apple AAPL USD 100 228 IBKR
##### Tencent 0700.HK HKD 380 371.47 Futu
##### Kweichow Moutai 600519.SS CNY 10 1680 China Securities
##### E Fund Blue Chip 110011 40000 3.99 Alipay
##### USD Cash 50000 Schwab
`;

// Trigger a browser download of a Blob or string.
export function downloadFile(
  content: Blob | string,
  filename: string,
  mime = "text/markdown",
): void {
  const blob =
    typeof content === "string"
      ? new Blob([content], { type: mime })
      : content;
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
