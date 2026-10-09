"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { setJadeCadence, type BriefingPlan, type Me } from "@/lib/api";
import { useSubscription } from "@/app/profile/_components/use-subscription";
import { SubscriptionDialog } from "@/app/profile/_components/subscription-dialog";

export function JadePageBody({ me, hadLoadError }: { me: Me | null; hadLoadError: boolean }) {
  const t = useTranslations("jade");
  const profile = useTranslations("profile");
  const router = useRouter();
  const subscription = useSubscription(me?.subscription);
  const [cadencePending, setCadencePending] = useState(false);
  const [cadenceError, setCadenceError] = useState(false);
  const [savedCadence, setSavedCadence] = useState<{ source: string; value: BriefingPlan } | null>(null);
  if (hadLoadError || !me) return <p className="text-sm text-destructive" role="alert">{profile("errorLoadFailed")}</p>;

  const isJade = me.subscription.status === "active" && me.subscription.type === "jade";
  const unverified = me.email_verified_at == null && me.delivery_email_verified_at == null;
  const cadence = savedCadence?.source === me.subscription.cadence ? savedCadence.value : me.subscription.cadence;
  async function changeCadence(value: BriefingPlan) {
    if (cadencePending || !me) return;
    setCadencePending(true);
    setCadenceError(false);
    try {
      await setJadeCadence(value);
      setSavedCadence({ source: me.subscription.cadence, value });
      router.refresh();
    } catch {
      setCadenceError(true);
    } finally {
      setCadencePending(false);
    }
  }
  return <div className="flex min-w-0 flex-col gap-6">
    <SubscriptionDialog state={subscription} />
    <Card><CardHeader><h1 className="font-heading text-2xl font-medium">{t("title")}</h1></CardHeader>
      <CardContent className="flex flex-col gap-3 px-4 text-sm"><p>{t("intro")}</p><p>{t("price")}</p></CardContent>
    </Card>
    <Card><CardHeader><CardTitle>{profile("subscriptionLabel")}</CardTitle></CardHeader>
      <CardContent className="flex flex-wrap items-center gap-3 px-4 text-sm">
        {isJade ? <>
          <p>{profile(me.subscription.cancel_pending ? "subscriptionEnds" : "subscriptionRenews", { plan: profile("subscriptionTitle.jade"), expires_on: me.subscription.expires_on ?? "" })}</p>
          <Button variant="outline" disabled={subscription.pending} onClick={() => void subscription.choose(me.subscription.cancel_pending ? "jade" : "cancel")}>{profile(me.subscription.cancel_pending ? "subscriptionResume" : "subscriptionCancel")}</Button>
        </> : <>
          {me.subscription.status === "expired" && <Badge className="h-auto whitespace-normal">{profile("subscriptionExpired", { expires_on: me.subscription.expires_on ?? "" })}</Badge>}
          <Button disabled={unverified || subscription.pending} onClick={() => void subscription.choose("jade")}>{t("subscribe")}</Button>
          {unverified && <p className="text-muted-foreground">{profile("subscriptionVerifyEmail")}</p>}
        </>}
        {subscription.error && !subscription.dialog && <p role="alert" className="w-full text-destructive">{subscription.error}</p>}
      </CardContent>
    </Card>
    {isJade && <Card><CardHeader><CardTitle>{profile("reportScheduleHeading")}</CardTitle></CardHeader>
      <CardContent className="flex min-w-0 flex-col gap-3 px-4 text-sm">
        <select aria-label={profile("reportScheduleHeading")} className="w-full min-w-0 rounded-md border border-white/10 bg-transparent px-2 py-1.5 text-sm" disabled={cadencePending} value={cadence} onChange={(event) => void changeCadence(event.target.value as BriefingPlan)}>
          <option value="weekly">{profile("reportScheduleOptions.weekly")}</option><option value="mwf">{profile("reportScheduleOptions.everyOtherDay")}</option><option value="daily">{profile("reportScheduleOptions.daily")}</option>
        </select>
        <p className="text-muted-foreground">{t("cadenceNote")}</p>
        {(cadence === "mwf" || cadence === "daily") && !me.has_holdings && <p>{profile("subscriptionNeedsHoldings")}</p>}
        {cadenceError && <p role="alert" className="text-destructive">{t("cadenceError")}</p>}
      </CardContent>
    </Card>}
  </div>;
}
