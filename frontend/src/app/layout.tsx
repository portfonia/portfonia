import { getRouteLocale } from "@/lib/seo-server";
import { SITE_URL } from "@/lib/seo";
import { catalogs } from "@/locales";
import type { Metadata } from "next";
import { Geist, Geist_Mono, Newsreader } from "next/font/google";
import "./globals.css";

import { AppShell } from "./_components/app-shell";
import { LocaleProvider } from "./_components/locale-provider";
import { SiteHeader } from "@/components/site-header";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

const newsreader = Newsreader({
  variable: "--font-newsreader",
  subsets: ["latin"],
  style: ["normal", "italic"],
});

export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  title: "Portfonia",
  description: catalogs.en.seo.pages.home.description,
};

export default async function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  const routeLocale = await getRouteLocale();
  return (
    <html
      lang={routeLocale ?? "en"}
      className={`dark ${geistSans.variable} ${geistMono.variable} ${newsreader.variable} h-full antialiased`}
    >
      <body className="min-h-full flex flex-col">
        <LocaleProvider routeLocale={routeLocale}>
          <AppShell>
            <SiteHeader />
            {children}
          </AppShell>
        </LocaleProvider>
      </body>
    </html>
  );
}
