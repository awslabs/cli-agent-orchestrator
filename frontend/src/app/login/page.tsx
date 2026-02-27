"use client";

import { FormEvent, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { caoRequest } from "@/lib/cao";

export default function LoginPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const nextPath = searchParams.get("next") || "/dashboard";

  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    let canceled = false;

    async function checkExistingLogin() {
      const result = await caoRequest<{ authenticated: boolean }>("GET", "/auth/me");
      if (!canceled && result.ok && result.data.authenticated) {
        router.replace(nextPath);
      }
    }

    checkExistingLogin();
    return () => {
      canceled = true;
    };
  }, [nextPath, router]);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setError("");

    const result = await caoRequest<{ ok: boolean }>("POST", "/auth/login", {
      body: { password },
    });

    if (!result.ok) {
      setError("登录失败：密码错误或服务不可用");
      setSubmitting(false);
      return;
    }

    router.replace(nextPath);
  }

  return (
    <main
      style={{
        minHeight: "100vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 24,
      }}
    >
      <div
        style={{
          width: 360,
          background: "var(--surface)",
          border: "1px solid var(--border)",
          borderRadius: 10,
          padding: 22,
        }}
      >
        <h1 style={{ marginBottom: 10, color: "var(--text-bright)", fontSize: 20 }}>CAO 控制台登录</h1>
        <p style={{ marginBottom: 16, color: "var(--text-dim)", fontSize: 13 }}>
          输入控制台密码后进入管理页面
        </p>

        <form onSubmit={onSubmit}>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="控制台密码"
            required
            style={{
              width: "100%",
              border: "1px solid var(--border)",
              borderRadius: 8,
              background: "var(--surface2)",
              color: "var(--text)",
              padding: "9px 12px",
              marginBottom: 10,
            }}
          />
          {error && <div style={{ color: "var(--danger)", marginBottom: 8, fontSize: 13 }}>{error}</div>}
          <button
            type="submit"
            disabled={submitting}
            style={{
              width: "100%",
              border: "none",
              borderRadius: 8,
              background: "var(--accent)",
              color: "white",
              padding: "9px 12px",
              fontWeight: 700,
              cursor: "pointer",
              opacity: submitting ? 0.75 : 1,
            }}
          >
            {submitting ? "登录中..." : "登录"}
          </button>
        </form>
      </div>
    </main>
  );
}
