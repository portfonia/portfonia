// Server Action redirect() throws a NEXT_REDIRECT error that the client
// promise surfaces as a rejection. That is success, not failure.
export function isNextRedirectError(error: unknown): boolean {
  if (typeof error !== "object" || error === null || !("digest" in error)) {
    return false;
  }
  const digest = error.digest;
  if (typeof digest !== "string") return false;
  const [code, type] = digest.split(";");
  return code === "NEXT_REDIRECT" && (type === "replace" || type === "push");
}
