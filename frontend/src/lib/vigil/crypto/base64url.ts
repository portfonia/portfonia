// Unpadded base64url (RFC 4648 §5) for every binary manifest field
// (#450 Design section 5's "Browser v1" contract: "Binary JSON unpadded base64url").
// btoa/atob operate on binary strings, not bytes directly, hence the
// charCode round-trip; this runs identically in a browser Worker and in
// Vitest's jsdom/node environments (both provide btoa/atob globally).

export function encodeBase64Url(bytes: Uint8Array): string {
  let binary = "";
  for (let i = 0; i < bytes.length; i += 1) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function decodeBase64Url(value: string): Uint8Array {
  if (/[+/=]/.test(value)) {
    throw new Error("not unpadded base64url: contains a standard-base64-only character");
  }
  const padded = value.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (value.length % 4)) % 4);
  const binary = atob(padded);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes;
}
