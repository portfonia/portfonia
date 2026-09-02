// Trigger a browser download of a Blob or string.

export function filenameFromContentDisposition(
  header: string | null,
  fallback: string,
): string {
  if (!header) return fallback;
  const star = /filename\*=(?:UTF-8''|utf-8'')([^;]+)/i.exec(header);
  if (star?.[1]) {
    try {
      return decodeURIComponent(star[1].replace(/["']/g, "").trim());
    } catch {
      return star[1].trim();
    }
  }
  const quoted = /filename="([^"]+)"/i.exec(header);
  if (quoted?.[1]) return quoted[1];
  const plain = /filename=([^;]+)/i.exec(header);
  if (plain?.[1]) return plain[1].trim().replace(/^["']|["']$/g, "");
  return fallback;
}

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
