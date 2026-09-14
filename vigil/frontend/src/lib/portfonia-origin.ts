export function configuredPortfoniaOrigin(): string {
  const raw = process.env.NEXT_PUBLIC_PORTFONIA_ORIGIN?.trim() || "https://portfonia.com";
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    throw new Error("NEXT_PUBLIC_PORTFONIA_ORIGIN must be an absolute URL");
  }
  if (url.protocol !== "https:") {
    throw new Error("NEXT_PUBLIC_PORTFONIA_ORIGIN must use https");
  }
  if (url.username || url.password || url.search || url.hash || url.pathname !== "/") {
    throw new Error("NEXT_PUBLIC_PORTFONIA_ORIGIN must be an origin");
  }
  return url.origin;
}
