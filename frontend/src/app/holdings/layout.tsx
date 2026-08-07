import { SiteHeader } from "@/components/site-header";

export default function HoldingsLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <>
      <SiteHeader />
      {children}
    </>
  );
}
