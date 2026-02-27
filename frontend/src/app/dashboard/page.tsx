"use client";

import { useEffect, useMemo, useState } from "react";

import ConsoleNav from "@/components/ConsoleNav";
import RequireAuth from "@/components/RequireAuth";
import { caoRequest, ConsoleAgent, ConsoleOverview } from "@/lib/cao";
import { toStatusLabel } from "@/lib/status";

function Card({ title, value }: { title: string; value: string | number }) {
  return (
    <div
      style={{
        background: "var(--surface)",
        border: "1px solid var(--border)",
        borderRadius: 8,
        padding: 14,
      }}
    >
      <div style={{ color: "var(--text-dim)", fontSize: 12, marginBottom: 6 }}>{title}</div>
      <div style={{ color: "var(--text-bright)", fontSize: 20, fontWeight: 700 }}>{value}</div>
    </div>
  );
}

function BarChartCard({
  title,
  rows,
}: {
  title: string;
  rows: Array<{ label: string; value: number }>;
}) {
  const total = rows.reduce((sum, row) => sum + row.value, 0) || 1;
  return (
    <div
      style={{
        background: "var(--surface)",
        border: "1px solid var(--border)",
        borderRadius: 12,
        padding: 14,
      }}
    >
      <div style={{ color: "var(--text-bright)", fontWeight: 700, marginBottom: 10 }}>{title}</div>
      {rows.length === 0 ? (
        <div style={{ color: "var(--text-dim)" }}>暂无数据</div>
      ) : (
        rows.map((row) => {
          const percent = Math.round((row.value / total) * 100);
          return (
            <div key={row.label} style={{ marginBottom: 10 }}>
              <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
                <span style={{ color: "var(--text)", fontSize: 13 }}>{row.label}</span>
                <span style={{ color: "var(--text-dim)", fontSize: 12 }}>
                  {row.value} ({percent}%)
                </span>
              </div>
              <div style={{ height: 8, borderRadius: 999, background: "var(--surface2)", overflow: "hidden" }}>
                <div
                  style={{
                    width: `${percent}%`,
                    height: "100%",
                    background: "var(--accent)",
                  }}
                />
              </div>
            </div>
          );
        })
      )}
    </div>
  );
}

function formatUptime(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  return `${h}h ${m}m ${s}s`;
}

export default function DashboardPage() {
  const [overview, setOverview] = useState<ConsoleOverview | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let canceled = false;

    async function fetchOverview() {
      const result = await caoRequest<ConsoleOverview>("GET", "/console/overview");
      if (canceled) {
        return;
      }
      if (!result.ok) {
        setError("获取控制台统计失败");
        return;
      }
      setOverview(result.data);
      setError("");
    }

    fetchOverview();
    const timer = setInterval(fetchOverview, 10000);
    return () => {
      canceled = true;
      clearInterval(timer);
    };
  }, []);

  const providerRows = Object.entries(overview?.provider_counts || {});
  const statusRows = Object.entries(overview?.status_counts || {});
  const mainAgents: ConsoleAgent[] = overview?.main_agents || [];
  const mainStatusRows = useMemo(() => {
    const counts = new Map<string, number>();
    mainAgents.forEach((agent) => {
      const key = agent.status || "unknown";
      counts.set(key, (counts.get(key) || 0) + 1);
    });
    return Array.from(counts.entries()).map(([label, value]) => ({
      label: toStatusLabel(label),
      value,
    }));
  }, [mainAgents]);

  return (
    <RequireAuth>
      <ConsoleNav />
      <main style={{ padding: 18 }}>
        <h1 style={{ fontSize: 22, color: "var(--text-bright)", marginBottom: 14 }}>集团总览</h1>
        {error && <div style={{ color: "var(--danger)", marginBottom: 12 }}>{error}</div>}

        <section
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
            gap: 12,
            marginBottom: 18,
          }}
        >
          <Card title="集团在岗员工" value={overview?.agents_total ?? "-"} />
          <Card title="在营团队数" value={overview?.main_agents_total ?? "-"} />
          <Card title="团队成员数" value={overview?.worker_agents_total ?? "-"} />
          <Card title="集团系统运行时长" value={overview ? formatUptime(overview.uptime_seconds) : "-"} />
        </section>

        <section
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))",
            gap: 12,
            marginBottom: 18,
          }}
        >
          <BarChartCard
            title="技术栈分布图"
            rows={providerRows.map(([label, value]) => ({ label, value }))}
          />
          <BarChartCard
            title="运行状态分布图"
            rows={statusRows.map(([label, value]) => ({ label: toStatusLabel(label), value }))}
          />
        </section>

        <section style={{ background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 12, padding: 12 }}>
          <div style={{ color: "var(--text-bright)", fontWeight: 700, marginBottom: 8 }}>团队负责人看板</div>
          {mainStatusRows.length > 0 && (
            <div style={{ marginBottom: 12 }}>
              <BarChartCard title="负责人状态分布" rows={mainStatusRows} />
            </div>
          )}
          {mainAgents.length === 0 ? (
            <div style={{ color: "var(--text-dim)" }}>当前没有在营团队</div>
          ) : (
            mainAgents.map((agent) => (
              <div
                key={agent.id}
                style={{
                  padding: "8px 10px",
                  border: "1px solid var(--border)",
                  borderRadius: 10,
                  marginBottom: 8,
                  background: "var(--surface2)",
                }}
              >
                <div style={{ color: "var(--text-bright)", fontFamily: "var(--mono)", fontSize: 12 }}>{agent.id}</div>
                <div style={{ color: "var(--text-dim)", fontSize: 12 }}>
                  会话标题：{agent.session_name || "-"} · {agent.agent_profile} · {agent.provider} · {toStatusLabel(agent.status)}
                </div>
              </div>
            ))
          )}
        </section>
      </main>
    </RequireAuth>
  );
}
