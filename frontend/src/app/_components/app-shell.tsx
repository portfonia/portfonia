"use client";

import { useLocale } from "./locale-provider";

export function AppShell({ children }: { children: React.ReactNode }) {
  const { locale } = useLocale();

  return (
    <div lang={locale} className="min-h-screen bg-background font-sans text-foreground">
      {children}
    </div>
  );
}
