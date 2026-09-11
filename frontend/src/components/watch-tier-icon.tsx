import { Circle, Star } from "lucide-react";
import { useTranslations } from "next-intl";

import type { WatchTier } from "@/lib/api";

// Small inline marker next to a holding's ticker/fund_code: hollow circle =
// watch, filled circle = focus, filled star = critical. Renders nothing for
// an unwatched holding (issue #430).
export function WatchTierIcon({ tier }: { tier: WatchTier | null | undefined }) {
  const t = useTranslations("holdings");
  if (!tier) return null;

  const label = t(`watchTier.${tier}`);
  const icon =
    tier === "watch" ? (
      <Circle className="size-3" aria-hidden="true" />
    ) : tier === "focus" ? (
      <Circle className="size-3" fill="currentColor" aria-hidden="true" />
    ) : (
      <Star className="size-3" fill="currentColor" aria-hidden="true" />
    );

  return (
    <span
      className="ml-1 inline-flex align-middle text-muted-foreground"
      role="img"
      aria-label={label}
      title={label}
    >
      {icon}
    </span>
  );
}
