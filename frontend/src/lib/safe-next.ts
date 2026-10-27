export function safeNext(raw: unknown): string {
  return typeof raw === "string" &&
    raw.startsWith("/") &&
    !raw.startsWith("//") &&
    !raw.startsWith("/\\") &&
    !/[\x00-\x1F\x7F]/.test(raw)
    ? raw
    : "/portfolio";
}
