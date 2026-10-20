import type { ReactNode } from "react";
import { Hourglass, Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";

export function CalculatingOverlay({ active, label, children, className, slow = false, slowNotice }: {
  active: boolean; label: string; children: ReactNode; className?: string; slow?: boolean; slowNotice?: string;
}) {
  return <div className={cn("relative min-w-0", className)} aria-busy={active || undefined}>
    <div inert={active || undefined} className="min-w-0">{children}</div>
    {active && <div role="status" aria-live="polite" data-testid="calculating-overlay"
      className="absolute inset-0 z-10 flex items-start justify-center rounded-md bg-background/70 pt-16 backdrop-blur-[1px]">
      <span className="flex items-center gap-2 rounded-md border bg-card px-3 py-2 text-sm shadow-sm">
        {slow ? <><Hourglass className="size-4" aria-hidden="true" /><span>{label}<span className="block">{slowNotice}</span></span></> : <><Loader2 className="size-4 animate-spin" aria-hidden="true" />{label}</>}
      </span>
    </div>}
  </div>;
}
