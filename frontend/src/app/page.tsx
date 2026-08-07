import type { Metadata } from "next";

import { HomeNav } from "./_components/home-nav";
import { HomeSections } from "./_components/home-sections";
import { LocaleProvider } from "./_components/locale-provider";

export const metadata: Metadata = {
  title: "Portfonia — What's worth noticing",
};

export default function HomePage() {
  return (
    <div className="dark min-h-screen bg-background font-sans text-foreground">
      <LocaleProvider>
        <HomeNav />
        <HomeSections />
      </LocaleProvider>
    </div>
  );
}
