"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useSession } from "@/hooks/use-session";
import { listApiTokens, createApiToken, revokeApiToken, ApiError, type ApiToken } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { AlertDialog, AlertDialogContent, AlertDialogHeader, AlertDialogTitle, AlertDialogDescription, AlertDialogFooter, AlertDialogAction, AlertDialogCancel } from "@/components/ui/alert-dialog";

export function AgentBody() {
  const t = useTranslations("agent");
  const session = useSession();
  return <div className="flex flex-col gap-6">
    <h1 className="text-2xl font-semibold">{t("title")}</h1>
    <p>{t("intro")}</p>
    <section className="flex flex-col gap-2">
      <h2 className="text-lg font-medium">{t("limitsTitle")}</h2>
      <p>{t("limits")}</p>
      <p>{t("quiet")}</p>
      <p>{t("notice")}</p>
    </section>
    {session.status === "guest" && <p><Link href="/login" className="underline">{t("signIn")}</Link> {t("signInNote")}</p>}
    {session.status === "authed" && <TokenSettings key={session.email} />}
  </div>;
}

function formatTime(value: string | null, empty: string): string {
  if (!value) return empty;
  return new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(new Date(value)) + " ET";
}

function TokenSettings() {
  const t = useTranslations("agent");
  const [tokens, setTokens] = useState<ApiToken[]>([]);
  const [loading, setLoading] = useState(true);
  const [name, setName] = useState("");
  const [expires, setExpires] = useState("");
  const [shown, setShown] = useState<string | null>(null);
  const [selected, setSelected] = useState<ApiToken | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void listApiTokens().then(rows => { if (!cancelled) setTokens(rows); }).catch(() => { if (!cancelled) setError(t("error")); }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [t]);

  async function create(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending) return;
    setPending(true); setError(null); setCopied(false); setShown(null);
    try {
      const result = await createApiToken(name, expires || null);
      setShown(result.token); setName(""); setExpires("");
      setTokens(await listApiTokens());
    } catch (err) {
      setError(err instanceof ApiError && err.status === 409 ? t("tokenLimit") : t("error"));
    } finally { setPending(false); }
  }

  async function revoke() {
    if (!selected || pending) return;
    setPending(true); setError(null);
    try {
      await revokeApiToken(selected.id);
      setSelected(null); setShown(null);
      setTokens(await listApiTokens());
    } catch { setError(t("error")); }
    finally { setPending(false); }
  }

  async function copy() {
    if (!shown) return;
    try { await navigator.clipboard.writeText(shown); setCopied(true); }
    catch { setError(t("copyError")); }
  }

  return <section className="flex flex-col gap-4">
    <h2 className="text-lg font-medium">{t("settingsTitle")}</h2>
    <p className="text-sm text-foreground/70">{t("tokenRule")}</p>
    <form onSubmit={event => void create(event)} className="flex flex-col gap-3">
      <label htmlFor="agent-token-name">{t("name")}</label>
      <Input id="agent-token-name" required maxLength={50} value={name} onChange={event => setName(event.target.value)} />
      <label htmlFor="agent-token-expiry">{t("expiry")}</label>
      <Input id="agent-token-expiry" type="date" value={expires} onChange={event => setExpires(event.target.value)} />
      <p className="text-xs text-foreground/70">{t("expiryNote")}</p>
      <Button type="submit" disabled={pending || loading || !name.trim()}>{t("create")}</Button>
    </form>
    {error && <p role="alert" className="text-destructive">{error}</p>}
    {shown && <div className="rounded-lg border border-border p-4 flex flex-col gap-3">
      <p>{t("showOnce")}</p>
      <code className="break-all select-all">{shown}</code>
      <div className="flex gap-2"><Button onClick={() => void copy()}>{copied ? t("copied") : t("copy")}</Button><Button variant="outline" onClick={() => setShown(null)}>{t("hide")}</Button></div>
    </div>}
    {loading ? <p>{t("loading")}</p> : tokens.length === 0 ? <p>{t("empty")}</p> : <ul className="flex flex-col gap-3">
      {tokens.map(token => <li key={token.id} className="rounded-lg border border-border p-4 flex flex-col gap-2">
        <div className="flex items-center justify-between gap-3"><span className="font-medium">{token.name}</span><Button variant="outline" disabled={pending} onClick={() => setSelected(token)}>{t("revoke")}</Button></div>
        <code>{token.prefix}</code>
        <p>{t(`statuses.${token.status}`)}</p>
        <dl className="text-sm text-foreground/70 grid grid-cols-2 gap-1">
          <dt>{t("created")}</dt><dd>{formatTime(token.created_at, t("never"))}</dd>
          <dt>{t("lastUsed")}</dt><dd>{formatTime(token.last_used_at, t("never"))}</dd>
          <dt>{t("expires")}</dt><dd>{formatTime(token.expires_at, t("noExpiry"))}</dd>
        </dl>
      </li>)}
    </ul>}
    <AlertDialog open={selected !== null} onOpenChange={open => { if (!open && !pending) setSelected(null); }}>
      <AlertDialogContent>
        <AlertDialogHeader><AlertDialogTitle>{t("revokeTitle")}</AlertDialogTitle><AlertDialogDescription>{t("revokeBody", { name: selected?.name ?? "" })}</AlertDialogDescription></AlertDialogHeader>
        <AlertDialogFooter><AlertDialogCancel disabled={pending}>{t("cancel")}</AlertDialogCancel><AlertDialogAction disabled={pending} onClick={() => void revoke()}>{t("confirmRevoke")}</AlertDialogAction></AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  </section>;
}
