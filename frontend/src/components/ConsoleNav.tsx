"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";

import { caoRequest } from "@/lib/cao";

const navItems = [
  { href: "/dashboard", label: "集团总览" },
  { href: "/organization", label: "组织管理" },
  { href: "/agents", label: "Agent 管理" },
];

export default function ConsoleNav() {
  const pathname = usePathname();
  const router = useRouter();

  async function handleLogout() {
    await caoRequest("POST", "/auth/logout");
    router.replace("/login");
  }

  return (
    <header
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        padding: "12px 18px",
        borderBottom: "1px solid var(--border)",
        background: "var(--surface)",
      }}
    >
      <div style={{ fontWeight: 700, color: "var(--text-bright)" }}>CAO Console</div>
      <nav style={{ display: "flex", gap: 16 }}>
        {navItems.map((item) => {
          const active = pathname === item.href;
          return (
            <Link
              key={item.href}
              href={item.href}
              style={{
                textDecoration: "none",
                color: active ? "var(--text-bright)" : "var(--text-dim)",
                fontWeight: active ? 700 : 500,
              }}
            >
              {item.label}
            </Link>
          );
        })}
      </nav>
      <button
        onClick={handleLogout}
        style={{
          border: "1px solid var(--border)",
          background: "var(--surface2)",
          color: "var(--text)",
          borderRadius: 6,
          padding: "6px 10px",
          cursor: "pointer",
        }}
      >
        退出登录
      </button>
    </header>
  );
}
