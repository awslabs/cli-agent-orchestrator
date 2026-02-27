"use client";

import { useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";

import { caoRequest } from "@/lib/cao";

export default function RequireAuth({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [ready, setReady] = useState(false);

  useEffect(() => {
    let canceled = false;

    async function checkAuth() {
      const result = await caoRequest<{ authenticated: boolean }>("GET", "/auth/me");
      if (canceled) {
        return;
      }

      if (!result.ok || !result.data.authenticated) {
        router.replace(`/login?next=${encodeURIComponent(pathname || "/dashboard")}`);
        return;
      }

      setReady(true);
    }

    checkAuth();

    return () => {
      canceled = true;
    };
  }, [pathname, router]);

  if (!ready) {
    return <div style={{ padding: 24, color: "var(--text-dim)" }}>Checking login status...</div>;
  }

  return <>{children}</>;
}
