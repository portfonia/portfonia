"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";

export function RevokeResult({ token }: { token: string }) {
  const t = useTranslations("agent.revokePage");
  const request = useRef<Promise<boolean> | null>(null);
  const [result, setResult] = useState<boolean | null>(null);
  useEffect(() => {
    let cancelled = false;
    if (request.current === null) {
      request.current = fetch("/api/api-tokens/revoke-by-link", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token }) }).then(response => response.ok).catch(() => false);
    }
    void request.current.then(ok => { if (!cancelled) setResult(ok); });
    return () => { cancelled = true; };
  }, [token]);
  if (result === null) return <p>{t("pending")}</p>;
  return <p role={result ? "status" : "alert"}>{t(result ? "success" : "failure")}</p>;
}
