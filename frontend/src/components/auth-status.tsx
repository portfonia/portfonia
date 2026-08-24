"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

import { logout } from "@/lib/auth-actions";
import { createClient } from "@/lib/supabase/browser";
import { Button } from "@/components/ui/button";

// undefined = still checking (first paint, avoid a login/logout flash);
// null = confirmed logged out; string = the logged-in user's email.
type SessionEmail = string | null | undefined;

function useSessionEmail(): SessionEmail {
  const [email, setEmail] = useState<SessionEmail>(undefined);

  useEffect(() => {
    const supabase = createClient();
    let cancelled = false;

    supabase.auth.getUser().then(({ data }) => {
      if (!cancelled) setEmail(data.user?.email ?? null);
    });

    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((_event, session) => {
      setEmail(session?.user.email ?? null);
    });

    return () => {
      cancelled = true;
      subscription.unsubscribe();
    };
  }, []);

  return email;
}

export function AuthStatus({
  loginLabel,
  logoutLabel,
}: {
  loginLabel: string;
  logoutLabel: string;
}) {
  const email = useSessionEmail();

  if (email === undefined) return null;

  if (email === null) {
    return (
      <Link href="/login" className="text-sm text-foreground/70 hover:text-foreground">
        {loginLabel}
      </Link>
    );
  }

  return (
    <div className="flex items-center gap-3">
      <span className="hidden text-sm text-foreground/60 sm:inline">{email}</span>
      <form action={logout}>
        <Button type="submit" variant="outline" size="sm">
          {logoutLabel}
        </Button>
      </form>
    </div>
  );
}
