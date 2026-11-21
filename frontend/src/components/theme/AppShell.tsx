"use client";

import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { Sidebar } from "@/components/layout/Sidebar";
import { MainContent } from "@/components/theme/MainContent";
import { getAuthStatus } from "@/lib/api";

/** Keep the authentication screen outside the authenticated application shell.
 *
 * The session is re-checked on every navigation, but once it has been confirmed
 * the shell (sidebar) stays mounted, so the sidebar keeps its scroll position
 * when you tap a menu item. A failed check still redirects to the login page. */
export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [authenticated, setAuthenticated] = useState(false);

  useEffect(() => {
    if (pathname === "/login") return;

    let active = true;
    getAuthStatus()
      .then((result) => {
        if (!active) return;
        if (result.authenticated) {
          setAuthenticated(true);
        } else {
          setAuthenticated(false);
          router.replace(`/login?next=${encodeURIComponent(pathname || "/")}`);
        }
      })
      .catch(() => {
        if (!active) return;
        setAuthenticated(false);
        router.replace(`/login?next=${encodeURIComponent(pathname || "/")}`);
      });

    return () => {
      active = false;
    };
  }, [pathname, router]);

  if (pathname === "/login") {
    return <main className="flex-1 min-h-screen bg-background">{children}</main>;
  }
  if (!authenticated) {
    return <main className="flex-1 min-h-screen bg-background" aria-busy="true" />;
  }
  return (
    <>
      <Sidebar />
      <MainContent>{children}</MainContent>
    </>
  );
}
