"use client";

import { usePathname } from "next/navigation";

import { useLocale } from "./locale-provider";

export function AppShell({ children }: { children: React.ReactNode }) {
  const { locale } = useLocale();
  const pathname = usePathname();
  // messages.ts (holdings, and every other non-home route) is English-only —
  // stamping the stored locale here would mislabel English content as zh for
  // screen readers / in-browser translate. Only home actually renders in the
  // selected locale (home-messages.ts).
  const lang = pathname === "/" ? locale : "en";

  return (
    <div lang={lang} className="min-h-screen bg-background font-sans text-foreground">
      {children}
    </div>
  );
}
