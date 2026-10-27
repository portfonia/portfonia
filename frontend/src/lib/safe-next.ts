export function safeNext(raw: unknown): string {
  return typeof raw === "string" &&
    raw.startsWith("/") &&
    !raw.startsWith("//") &&
    !raw.startsWith("/\\") &&
    !/[\r\n]/.test(raw)
    ? raw
    : "/portfolio";
}
