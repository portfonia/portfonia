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
