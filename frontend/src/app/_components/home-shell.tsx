"use client";

import { useLocale } from "./locale-provider";

export function HomeShell({ children }: { children: React.ReactNode }) {
  const { locale } = useLocale();

  return (
    <div
      lang={locale}
      className="dark min-h-screen bg-background font-sans text-foreground"
      style={{ "--radius": "0.75rem" } as React.CSSProperties}
    >
      {children}
    </div>
  );
}
