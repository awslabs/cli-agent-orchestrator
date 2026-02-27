"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";

import ConsoleNav from "@/components/ConsoleNav";
import RequireAuth from "@/components/RequireAuth";
import {
  caoRequest,
  ConsoleAgent,
  ConsoleAgentProfilesResponse,
  CreateAgentProfileRequest,
  CreateAgentProfileResponse,
  ConsoleOrganization,
  InstallAgentProfileResponse,
} from "@/lib/cao";
import { toStatusLabel } from "@/lib/status";

const builtInProfiles = ["code_supervisor", "developer", "reviewer"];

const providers = [
  "",
  "kiro_cli",
  "claude_code",
  "codex",
  "q_cli",
  "qoder_cli",
  "opencode",
  "codebuddy",
  "copilot",
];

export default function OrganizationPage() {
  const [data, setData] = useState<ConsoleOrganization | null>(null);
  const [error, setError] = useState("");
  const [profileOptions, setProfileOptions] = useState<string[]>([]);

  const [mainProfile, setMainProfile] = useState("");
  const [mainProvider, setMainProvider] = useState("");
  const [creatingMain, setCreatingMain] = useState(false);

  const [workerProfile, setWorkerProfile] = useState("");
  const [workerProvider, setWorkerProvider] = useState("");
  const [workerLeaderId, setWorkerLeaderId] = useState("");
  const [creatingWorker, setCreatingWorker] = useState(false);

  const [newAgentName, setNewAgentName] = useState("");
  const [newAgentDescription, setNewAgentDescription] = useState("");
  const [newAgentProvider, setNewAgentProvider] = useState("");
  const [newAgentPrompt, setNewAgentPrompt] = useState("");
  const [creatingProfile, setCreatingProfile] = useState(false);

  const loadOrganization = useCallback(async () => {
    const result = await caoRequest<ConsoleOrganization>("GET", "/console/organization");
    if (!result.ok) {
      setError("获取组织结构失败");
      return;
    }
    setData(result.data);
    setError("");
  }, []);

  const loadProfileOptions = useCallback(async () => {
    const result = await caoRequest<ConsoleAgentProfilesResponse>(
      "GET",
      "/console/agent-profiles"
    );
    if (!result.ok) {
      setProfileOptions(builtInProfiles);
      setError("获取 Agent 类型列表失败，已回退内置类型");
      return;
    }
    const profiles = Array.from(
      new Set([...(result.data.profiles || []), ...builtInProfiles])
    ).sort();
    setProfileOptions(profiles);

    if (!mainProfile) {
      const preferredMain = profiles.includes("code_supervisor")
        ? "code_supervisor"
        : profiles[0] || "";
      setMainProfile(preferredMain);
    }
    if (!workerProfile) {
      const preferredWorker = profiles.includes("developer")
        ? "developer"
        : profiles[0] || "";
      setWorkerProfile(preferredWorker);
    }
  }, [mainProfile, workerProfile]);

  async function onboardNewEmployee(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setCreatingProfile(true);
    setError("");

    const body: CreateAgentProfileRequest = {
      name: newAgentName.trim(),
      description: newAgentDescription.trim(),
      system_prompt: newAgentPrompt.trim(),
    };
    if (newAgentProvider) {
      body.provider = newAgentProvider;
    }

    const result = await caoRequest<CreateAgentProfileResponse>(
      "POST",
      "/console/agent-profiles",
      { body }
    );

    if (!result.ok) {
      setError("创建自定义 Agent 类型失败，请检查名称是否重复或格式是否正确");
      setCreatingProfile(false);
      return;
    }

    const profileName = result.data.profile;
    const installResult = await caoRequest<InstallAgentProfileResponse>(
      "POST",
      `/console/agent-profiles/${profileName}/install`
    );
    if (!installResult.ok || !installResult.data.ok) {
      setError("岗位档案已保存，但安装失败，请检查后端日志");
      setCreatingProfile(false);
      return;
    }

    setNewAgentName("");
    setNewAgentDescription("");
    setNewAgentProvider("");
    setNewAgentPrompt("");
    setCreatingProfile(false);
    await loadProfileOptions();
  }

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadOrganization();
    const bootstrapTimer = setTimeout(() => {
      void loadProfileOptions();
    }, 0);
    const timer = setInterval(() => {
      void loadOrganization();
    }, 10000);
    return () => {
      clearInterval(timer);
      clearTimeout(bootstrapTimer);
    };
  }, [loadOrganization, loadProfileOptions]);

  async function createMainAgent(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setCreatingMain(true);
    setError("");

    const body: {
      role_type: "main";
      agent_profile: string;
      provider?: string;
    } = {
      role_type: "main",
      agent_profile: mainProfile.trim(),
    };

    if (mainProvider) {
      body.provider = mainProvider;
    }

    const result = await caoRequest("POST", "/console/organization/create", { body });
    if (!result.ok) {
      setError("创建主控 Agent 失败");
      setCreatingMain(false);
      return;
    }

    setCreatingMain(false);
    await loadOrganization();
  }

  async function createWorkerAgent(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setCreatingWorker(true);
    setError("");

    const body: {
      role_type: "worker";
      agent_profile: string;
      provider?: string;
      leader_id?: string;
    } = {
      role_type: "worker",
      agent_profile: workerProfile.trim(),
    };

    if (workerProvider) {
      body.provider = workerProvider;
    }
    if (workerLeaderId) {
      body.leader_id = workerLeaderId;
    }

    const result = await caoRequest("POST", "/console/organization/create", { body });
    if (!result.ok) {
      setError("创建 Worker Agent 失败");
      setCreatingWorker(false);
      return;
    }

    setCreatingWorker(false);
    await loadOrganization();
  }

  const groups = data?.leader_groups ?? [];
  const leaders = data?.leaders ?? [];

  return (
    <RequireAuth>
      <ConsoleNav />
      <main style={{ padding: 18 }}>
        <h1 style={{ fontSize: 22, color: "var(--text-bright)", marginBottom: 12 }}>组织管理</h1>

        {error && <div style={{ color: "var(--danger)", marginBottom: 12 }}>{error}</div>}

        <section
          style={{
            background: "var(--surface)",
            border: "1px solid var(--border)",
            borderRadius: 8,
            padding: 14,
            marginBottom: 14,
          }}
        >
          <div style={{ fontWeight: 700, color: "var(--text-bright)", marginBottom: 10 }}>入职新员工（新增岗位类型）</div>
          <form onSubmit={onboardNewEmployee}>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, marginBottom: 10 }}>
              <input
                value={newAgentName}
                onChange={(e) => setNewAgentName(e.target.value)}
                required
                placeholder="name，例如 data_analyst"
                style={{ border: "1px solid var(--border)", borderRadius: 6, background: "var(--surface2)", color: "var(--text)", padding: "8px 10px" }}
              />
              <input
                value={newAgentDescription}
                onChange={(e) => setNewAgentDescription(e.target.value)}
                required
                placeholder="description"
                style={{ border: "1px solid var(--border)", borderRadius: 6, background: "var(--surface2)", color: "var(--text)", padding: "8px 10px" }}
              />
            </div>
            <div style={{ marginBottom: 10 }}>
              <select
                value={newAgentProvider}
                onChange={(e) => setNewAgentProvider(e.target.value)}
                style={{ width: "100%", border: "1px solid var(--border)", borderRadius: 6, background: "var(--surface2)", color: "var(--text)", padding: "8px 10px" }}
              >
                {providers.map((item) => (
                  <option key={item || "default-new-profile"} value={item}>
                    {item || "不指定 provider（按系统默认）"}
                  </option>
                ))}
              </select>
            </div>
            <textarea
              value={newAgentPrompt}
              onChange={(e) => setNewAgentPrompt(e.target.value)}
              required
              placeholder="系统提示词（markdown 内容）"
              style={{ width: "100%", minHeight: 120, border: "1px solid var(--border)", borderRadius: 6, background: "var(--surface2)", color: "var(--text)", padding: "8px 10px", marginBottom: 10 }}
            />
            <button
              type="submit"
              disabled={creatingProfile}
              style={{ border: "none", borderRadius: 6, background: "var(--accent)", color: "#fff", padding: "8px 14px", cursor: "pointer", fontWeight: 700 }}
            >
              {creatingProfile ? "办理入职中..." : "保存岗位并完成安装"}
            </button>
          </form>
        </section>

        <section
          style={{
            background: "var(--surface)",
            border: "1px solid var(--border)",
            borderRadius: 8,
            padding: 14,
            marginBottom: 14,
          }}
        >
          <div style={{ fontWeight: 700, color: "var(--text-bright)", marginBottom: 10 }}>组建新团队（启动团队负责人）</div>
          <form onSubmit={createMainAgent} style={{ display: "grid", gridTemplateColumns: "1fr 1fr auto", gap: 10 }}>
            <select
              value={mainProfile}
              onChange={(e) => setMainProfile(e.target.value)}
              required
              style={{ border: "1px solid var(--border)", borderRadius: 6, background: "var(--surface2)", color: "var(--text)", padding: "8px 10px" }}
            >
              <option value="">请选择 Agent 类型</option>
              {profileOptions.map((profileName) => (
                <option key={`main-${profileName}`} value={profileName}>
                  {profileName}
                </option>
              ))}
            </select>
            <select
              value={mainProvider}
              onChange={(e) => setMainProvider(e.target.value)}
              style={{ border: "1px solid var(--border)", borderRadius: 6, background: "var(--surface2)", color: "var(--text)", padding: "8px 10px" }}
            >
              {providers.map((item) => (
                <option key={item || "default-main"} value={item}>
                  {item || "自动选择 provider"}
                </option>
              ))}
            </select>
            <button
              type="submit"
              disabled={creatingMain}
              style={{ border: "none", borderRadius: 6, background: "var(--accent)", color: "#fff", padding: "8px 14px", cursor: "pointer", fontWeight: 700 }}
            >
              {creatingMain ? "组建中..." : "启动团队"}
            </button>
          </form>
        </section>

        <section
          style={{
            background: "var(--surface)",
            border: "1px solid var(--border)",
            borderRadius: 8,
            padding: 14,
            marginBottom: 14,
          }}
        >
          <div style={{ fontWeight: 700, color: "var(--text-bright)", marginBottom: 10 }}>团队增员（入职执行员工）</div>
          <form onSubmit={createWorkerAgent} style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr auto", gap: 10 }}>
            <select
              value={workerProfile}
              onChange={(e) => setWorkerProfile(e.target.value)}
              required
              style={{ border: "1px solid var(--border)", borderRadius: 6, background: "var(--surface2)", color: "var(--text)", padding: "8px 10px" }}
            >
              <option value="">请选择 Agent 类型</option>
              {profileOptions.map((profileName) => (
                <option key={`worker-${profileName}`} value={profileName}>
                  {profileName}
                </option>
              ))}
            </select>
            <select
              value={workerProvider}
              onChange={(e) => setWorkerProvider(e.target.value)}
              style={{ border: "1px solid var(--border)", borderRadius: 6, background: "var(--surface2)", color: "var(--text)", padding: "8px 10px" }}
            >
              {providers.map((item) => (
                <option key={item || "default-worker"} value={item}>
                  {item || "自动选择 provider"}
                </option>
              ))}
            </select>
            <select
              value={workerLeaderId}
              onChange={(e) => setWorkerLeaderId(e.target.value)}
              style={{ border: "1px solid var(--border)", borderRadius: 6, background: "var(--surface2)", color: "var(--text)", padding: "8px 10px" }}
            >
              <option value="">不分配团队（独立团队编制）</option>
              {leaders.map((leader: ConsoleAgent) => (
                <option key={leader.id} value={leader.id}>
                  {leader.id} · {leader.agent_profile}
                </option>
              ))}
            </select>
            <button
              type="submit"
              disabled={creatingWorker}
              style={{ border: "none", borderRadius: 6, background: "var(--success)", color: "#fff", padding: "8px 14px", cursor: "pointer", fontWeight: 700 }}
            >
              {creatingWorker ? "办理中..." : "办理入职"}
            </button>
          </form>
        </section>

        <section style={{ background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 8, padding: 14 }}>
          <div style={{ fontWeight: 700, color: "var(--text-bright)", marginBottom: 10 }}>集团团队架构（负责人 → 员工）</div>
          {groups.length === 0 ? (
            <div style={{ color: "var(--text-dim)" }}>暂无团队</div>
          ) : (
            groups.map((group) => (
              <div key={group.leader.id} style={{ border: "1px solid var(--border)", borderRadius: 8, padding: 10, marginBottom: 10 }}>
                <div style={{ marginBottom: 8 }}>
                  <span style={{ color: "var(--text-bright)", fontWeight: 700 }}>{group.leader.id}</span>
                  <span style={{ color: "var(--text-dim)", marginLeft: 8 }}>
                    {group.leader.agent_profile} · {toStatusLabel(group.leader.status)}
                  </span>
                </div>
                {group.members.length === 0 ? (
                  <div style={{ color: "var(--text-dim)" }}>暂无直属 Worker</div>
                ) : (
                  <div style={{ overflowX: "auto" }}>
                    <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                      <thead>
                        <tr style={{ color: "var(--text-dim)", textAlign: "left" }}>
                          <th style={{ padding: "6px 8px" }}>Worker ID</th>
                          <th style={{ padding: "6px 8px" }}>Profile</th>
                          <th style={{ padding: "6px 8px" }}>Provider</th>
                          <th style={{ padding: "6px 8px" }}>状态</th>
                        </tr>
                      </thead>
                      <tbody>
                        {group.members.map((member) => (
                          <tr key={member.id} style={{ borderTop: "1px solid var(--border)" }}>
                            <td style={{ padding: "7px 8px", fontFamily: "var(--mono)", fontSize: 12 }}>{member.id}</td>
                            <td style={{ padding: "7px 8px" }}>{member.agent_profile}</td>
                            <td style={{ padding: "7px 8px" }}>{member.provider}</td>
                            <td style={{ padding: "7px 8px" }}>{toStatusLabel(member.status)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            ))
          )}
        </section>
      </main>
    </RequireAuth>
  );
}
