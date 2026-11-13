"use client";

import { usePathname } from "next/navigation";
import { useEffect } from "react";

import { useSession } from "@/hooks/use-session";
import { recordPageView } from "@/lib/api";

import { useLocale } from "./locale-provider";

export function AppShell({ children }: { children: React.ReactNode }) {
  const { locale } = useLocale();
  const session = useSession();
  const pathname = usePathname();

  useEffect(() => {
    if (session.status === "authed") {
      void recordPageView(pathname).catch(() => {});
    }
  }, [pathname, session.status]);

  return (
    <div lang={locale} className="min-h-screen bg-background font-sans text-foreground">
      {children}
    </div>
  );
}
