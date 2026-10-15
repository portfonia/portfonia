"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useLocale, useTranslations } from "next-intl";
import { type Locale } from "@/locales";
import { localizedPath } from "@/lib/seo";
import { useSession } from "@/hooks/use-session";
import { listApiTokens, createApiToken, revokeApiToken, ApiError, type ApiToken, type ApiTokenPermission } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { AlertDialog, AlertDialogContent, AlertDialogHeader, AlertDialogTitle, AlertDialogDescription, AlertDialogFooter, AlertDialogAction, AlertDialogCancel } from "@/components/ui/alert-dialog";

const AGENT_PATHS = {
  llms: "llms.txt",
  reference: "agent.md",
};

export function AgentBody() {
  const t = useTranslations("agent");
  const session = useSession();
  const locale = useLocale() as Locale;
  return <div className="flex flex-col gap-6">
    <h1 className="font-heading text-2xl font-medium">{t("title")}</h1>
    <p className="text-sm text-muted-foreground">{t("intro")}</p>
    <Link href={localizedPath("/agent/guide", locale)} className="text-sm underline">{t("guideLink")}</Link>
    <Card>
      <CardHeader><CardTitle role="heading" aria-level={2}>{t("docs.gettingStartedTitle")}</CardTitle></CardHeader>
      <CardContent className="flex flex-col gap-2">
        <p>{t("docs.gettingStarted")}</p>
        <p className="flex flex-wrap gap-4"><Link href="/llms.txt" className="underline">{AGENT_PATHS.llms}</Link><Link href="/agent.md" className="underline">{AGENT_PATHS.reference}</Link></p>
      </CardContent>
    </Card>
    <Card>
      <CardHeader><CardTitle role="heading" aria-level={2}>{t("docs.endpointsTitle")}</CardTitle></CardHeader>
      <CardContent className="flex flex-col gap-6">
        <p>{t("docs.endpointsLead")}</p>
        {(["token", "reports", "snapshots", "changes", "intel", "holdingsRead", "holdingsReplaceAll", "holdingsAdd", "holdingsReplace", "holdingsPatch", "holdingsDelete", "holdingsValidate"] as const).map(endpoint => (
          <section key={endpoint} className="flex flex-col gap-1">
            <h3 className="font-medium">{t(`docs.${endpoint}Heading`)}</h3>
            <code className="break-all">{t.raw(`docs.${endpoint}Path`)}</code>
            <p>{t(`docs.${endpoint}Access`)}</p>
            <p>{t(`docs.${endpoint}Action`)}</p>
            <p>{t(`docs.${endpoint}Note`)}</p>
          </section>
        ))}
        <p>{t("docs.endpointsClosing")}</p>
      </CardContent>
    </Card>
    <Card>
      <CardHeader><CardTitle role="heading" aria-level={2}>{t("limitsTitle")}</CardTitle></CardHeader>
      <CardContent className="flex flex-col gap-2">
        <p>{t("limits")}</p>
        <p>{t("quiet")}</p>
        <p>{t("notice")}</p>
        <p>{t("tokenRule")}</p>
      </CardContent>
    </Card>
    {session.status === "guest" && <p className="text-sm"><Link href="/login" className="underline">{t("signIn")}</Link> {t("signInNote")}</p>}
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
  const [permission, setPermission] = useState<ApiTokenPermission>("read_only");
  const [shown, setShown] = useState<string | null>(null);
  const [shownPermission, setShownPermission] = useState<ApiTokenPermission | null>(null);
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
      const result = await createApiToken(name, expires || null, permission);
      setShown(result.token); setShownPermission(result.permission); setName(""); setExpires(""); setPermission("read_only");
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
      setSelected(null); setShown(null); setShownPermission(null);
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
    <h2 className="font-heading text-base font-medium">{t("settingsTitle")}</h2>
    <form onSubmit={event => void create(event)} className="flex flex-col gap-3">
      <label htmlFor="agent-token-name">{t("name")}</label>
      <Input id="agent-token-name" required maxLength={50} value={name} onChange={event => setName(event.target.value)} />
      <label htmlFor="agent-token-expiry">{t("expiry")}</label>
      <Input id="agent-token-expiry" type="date" value={expires} onChange={event => setExpires(event.target.value)} />
      <p className="text-xs text-foreground/70">{t("expiryNote")}</p>
      <fieldset className="flex flex-col gap-2">
        <legend>{t("permissionLabel")}</legend>
        {(["read_only", "holdings_write"] as const).map(value => (
          <label key={value} className="flex items-center gap-2 text-sm">
            <input type="radio" name="agent-token-permission" value={value} checked={permission === value} onChange={() => setPermission(value)} />
            {t(`permissions.${value}`)}
          </label>
        ))}
      </fieldset>
      <p className="text-xs text-foreground/70">{t("permissionNote")}</p>
      <Button type="submit" disabled={pending || loading || !name.trim()}>{t("create")}</Button>
    </form>
    {error && <p role="alert" className="text-destructive">{error}</p>}
    {shown && <div className="rounded-lg border border-border p-4 flex flex-col gap-3">
      <p>{t("showOnce")}</p>
      {shownPermission && <p>{t(`permissions.${shownPermission}`)}</p>}
      <code className="break-all select-all">{shown}</code>
      <div className="flex gap-2"><Button onClick={() => void copy()}>{copied ? t("copied") : t("copy")}</Button><Button variant="outline" onClick={() => { setShown(null); setShownPermission(null); }}>{t("hide")}</Button></div>
    </div>}
    {loading ? <p>{t("loading")}</p> : tokens.length === 0 ? <p>{t("empty")}</p> : <ul className="flex flex-col gap-3">
      {tokens.map(token => <li key={token.id} className="rounded-lg border border-border p-4 flex flex-col gap-2">
        <div className="flex items-center justify-between gap-3"><span className="font-medium">{token.name}</span><Button variant="outline" disabled={pending} onClick={() => setSelected(token)}>{t("revoke")}</Button></div>
        <code>{token.prefix}</code>
        <p>{t(`permissions.${token.permission}`)}</p>
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
