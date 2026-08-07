import Link from "next/link";

import { messages } from "@/lib/messages";

export function SiteHeader() {
  return (
    <header className="sticky top-0 z-10 border-b bg-background/95 backdrop-blur supports-backdrop-filter:bg-background/80">
      <div className="mx-auto flex h-14 w-full max-w-5xl items-center px-6">
        <Link href="/" className="font-heading text-lg font-semibold tracking-tight">
          {messages.common.brandName}
        </Link>
      </div>
    </header>
  );
}
