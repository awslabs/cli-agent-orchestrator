"use client";

import React, { useState, useCallback } from "react";

// ─── Types ────────────────────────────────────────────────────────────────────

type Method = "GET" | "POST" | "DELETE";

interface ApiResult {
  ok: boolean;
  status: number;
  body: unknown;
  duration: number;
}

type PanelId =
  | "health"
  | "sessions-list"
  | "sessions-create"
  | "sessions-get"
  | "sessions-delete"
  | "session-terminals-list"
  | "session-terminals-create"
  | "terminals-get"
  | "terminals-workdir"
  | "terminals-input"
  | "terminals-output"
  | "terminals-exit"
  | "terminals-delete"
  | "inbox-send"
  | "inbox-list";

// ─── Helpers ──────────────────────────────────────────────────────────────────

function escapeHtml(s: string) {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function prettyJson(v: unknown): string {
  if (typeof v === "string") return v;
  return JSON.stringify(v, null, 2);
}

/** Call the Next.js middleware proxy at /api/cao/* */
async function caoFetch(
  method: Method,
  path: string,
  params?: Record<string, string>
): Promise<ApiResult> {
  const url = new URL("/api/cao" + path, window.location.origin);
  if (params) {
    Object.entries(params).forEach(([k, v]) => {
      if (v !== "" && v != null) url.searchParams.set(k, v);
    });
  }
  const t0 = Date.now();
  const res = await fetch(url.toString(), { method });
  const duration = Date.now() - t0;
  let body: unknown;
  try {
    body = await res.json();
  } catch {
    body = await res.text();
  }
  return { ok: res.ok, status: res.status, body, duration };
}

// ─── Sub-components ───────────────────────────────────────────────────────────

function ResultBox({ result }: { result: ApiResult | null }) {
  if (!result) return null;
  const text = prettyJson(result.body);
  return (
    <div style={{ marginTop: 12 }}>
      <pre
        style={{
          background: result.ok ? "#0d1f12" : "#1f0d0d",
          border: `1px solid ${result.ok ? "#3dbe7a44" : "#e0525244"}`,
          borderRadius: 6,
          padding: "10px 14px",
          fontSize: 12,
          fontFamily: "var(--mono)",
          color: result.ok ? "#c8f0d8" : "#f0c8c8",
          whiteSpace: "pre-wrap",
          wordBreak: "break-all",
          maxHeight: 400,
          overflow: "auto",
        }}
        dangerouslySetInnerHTML={{ __html: escapeHtml(text) }}
      />
      <div style={{ display: "flex", gap: 12, marginTop: 6, fontSize: 11, color: "var(--text-dim)" }}>
        <span style={{ color: result.ok ? "var(--success)" : "var(--danger)" }}>
          {result.ok ? "✓" : "✗"} HTTP {result.status}
        </span>
        <span>{result.duration}ms</span>
      </div>
    </div>
  );
}

function Field({
  label,
  required,
  children,
}: {
  label: string;
  required?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div style={{ marginBottom: 12 }}>
      <label
        style={{
          display: "block",
          fontSize: 12,
          color: "var(--text-dim)",
          marginBottom: 4,
          fontWeight: 600,
        }}
      >
        {label}{" "}
        {required && (
          <span style={{ color: "var(--danger)", fontWeight: 700 }}>*</span>
        )}
      </label>
      {children}
    </div>
  );
}

const inputStyle: React.CSSProperties = {
  width: "100%",
  padding: "7px 10px",
  borderRadius: 6,
  border: "1px solid var(--border)",
  background: "var(--surface2)",
  color: "var(--text)",
  fontFamily: "var(--mono)",
  fontSize: 12,
  outline: "none",
};

const selectStyle: React.CSSProperties = {
  ...inputStyle,
  cursor: "pointer",
};

const textareaStyle: React.CSSProperties = {
  ...inputStyle,
  resize: "vertical",
  minHeight: 80,
};

function Btn({
  onClick,
  danger,
  children,
}: {
  onClick: () => void;
  danger?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      style={{
        marginTop: 4,
        padding: "8px 18px",
        borderRadius: 6,
        border: "none",
        cursor: "pointer",
        fontWeight: 600,
        fontSize: 13,
        background: danger ? "var(--danger)" : "var(--accent)",
        color: "#fff",
        transition: "opacity .15s",
      }}
      onMouseEnter={(e) => ((e.target as HTMLElement).style.opacity = "0.85")}
      onMouseLeave={(e) => ((e.target as HTMLElement).style.opacity = "1")}
    >
      {children}
    </button>
  );
}

function CardWrap({
  method,
  path,
  desc,
  children,
}: {
  method: "GET" | "POST" | "DELETE";
  path: string;
  desc: string;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(true);
  const colors = {
    GET: { bg: "#1b3a6a", fg: "#4f8ef7" },
    POST: { bg: "#1b4a2a", fg: "#3dbe7a" },
    DELETE: { bg: "#4a1b1b", fg: "#e05252" },
  };
  const c = colors[method];
  return (
    <div
      style={{
        background: "var(--surface)",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius)",
        marginBottom: 16,
        overflow: "hidden",
      }}
    >
      <div
        onClick={() => setOpen((v) => !v)}
        style={{
          padding: "14px 18px",
          borderBottom: open ? "1px solid var(--border)" : "none",
          display: "flex",
          alignItems: "center",
          gap: 10,
          background: "var(--surface2)",
          cursor: "pointer",
        }}
      >
        <span
          style={{
            fontSize: 11,
            fontWeight: 700,
            padding: "2px 8px",
            borderRadius: 4,
            fontFamily: "var(--mono)",
            background: c.bg,
            color: c.fg,
            flexShrink: 0,
          }}
        >
          {method}
        </span>
        <span
          style={{
            fontSize: 13,
            fontWeight: 600,
            color: "var(--text-bright)",
            fontFamily: "var(--mono)",
          }}
        >
          {path}
        </span>
        <span style={{ fontSize: 12, color: "var(--text-dim)", flex: 1 }}>
          {desc}
        </span>
        <span style={{ color: "var(--text-dim)" }}>{open ? "▾" : "▸"}</span>
      </div>
      {open && <div style={{ padding: "16px 18px" }}>{children}</div>}
    </div>
  );
}

// ─── Panels ───────────────────────────────────────────────────────────────────

function HealthPanel() {
  const [result, setResult] = useState<ApiResult | null>(null);
  const run = useCallback(async () => {
    try {
      setResult(await caoFetch("GET", "/health"));
    } catch (e) {
      setResult({ ok: false, status: 0, body: String(e), duration: 0 });
    }
  }, []);
  return (
    <div>
      <p style={{ fontSize: 13, color: "var(--text-dim)", marginBottom: 16 }}>
        Check whether the CAO server is running and healthy.
      </p>
      <CardWrap method="GET" path="/health" desc="Server health">
        <Btn onClick={run}>Send Request</Btn>
        <ResultBox result={result} />
      </CardWrap>
    </div>
  );
}

function ListSessionsPanel() {
  const [result, setResult] = useState<ApiResult | null>(null);
  const run = useCallback(async () => {
    try {
      setResult(await caoFetch("GET", "/sessions"));
    } catch (e) {
      setResult({ ok: false, status: 0, body: String(e), duration: 0 });
    }
  }, []);
  return (
    <div>
      <p style={{ fontSize: 13, color: "var(--text-dim)", marginBottom: 16 }}>
        Retrieve all active tmux sessions managed by CAO.
      </p>
      <CardWrap method="GET" path="/sessions" desc="All sessions">
        <Btn onClick={run}>Send Request</Btn>
        <ResultBox result={result} />
      </CardWrap>
    </div>
  );
}

function CreateSessionPanel() {
  const [agentProfile, setAgentProfile] = useState("");
  const [provider, setProvider] = useState("");
  const [sessionName, setSessionName] = useState("");
  const [workingDir, setWorkingDir] = useState("");
  const [result, setResult] = useState<ApiResult | null>(null);

  const run = useCallback(async () => {
    if (!agentProfile.trim()) {
      alert("agent_profile is required");
      return;
    }
    try {
      setResult(
        await caoFetch("POST", "/sessions", {
          agent_profile: agentProfile,
          provider,
          session_name: sessionName,
          working_directory: workingDir,
        })
      );
    } catch (e) {
      setResult({ ok: false, status: 0, body: String(e), duration: 0 });
    }
  }, [agentProfile, provider, sessionName, workingDir]);

  return (
    <div>
      <p style={{ fontSize: 13, color: "var(--text-dim)", marginBottom: 16 }}>
        Create a new tmux session with one terminal running the specified agent.
      </p>
      <CardWrap method="POST" path="/sessions" desc="Create session">
        <Field label="agent_profile" required>
          <input
            style={inputStyle}
            value={agentProfile}
            onChange={(e) => setAgentProfile(e.target.value)}
            placeholder="e.g. coding-agent"
          />
        </Field>
        <Field label="provider (optional)">
          <select
            style={selectStyle}
            value={provider}
            onChange={(e) => setProvider(e.target.value)}
          >
            <option value="">— default (kiro_cli) —</option>
            <option value="kiro_cli">kiro_cli</option>
            <option value="q_cli">q_cli</option>
            <option value="claude_code">claude_code</option>
            <option value="codex">codex</option>
            <option value="qoder_cli">qoder_cli</option>
            <option value="opencode">opencode</option>
            <option value="codebuddy">codebuddy</option>
            <option value="copilot">copilot</option>
          </select>
        </Field>
        <Field label="session_name (optional)">
          <input
            style={inputStyle}
            value={sessionName}
            onChange={(e) => setSessionName(e.target.value)}
            placeholder="e.g. my-session"
          />
        </Field>
        <Field label="working_directory (optional)">
          <input
            style={inputStyle}
            value={workingDir}
            onChange={(e) => setWorkingDir(e.target.value)}
            placeholder="e.g. /home/user/project"
          />
        </Field>
        <Btn onClick={run}>Send Request</Btn>
        <ResultBox result={result} />
      </CardWrap>
    </div>
  );
}

function GetSessionPanel() {
  const [name, setName] = useState("");
  const [result, setResult] = useState<ApiResult | null>(null);
  const run = useCallback(async () => {
    if (!name.trim()) {
      alert("session_name is required");
      return;
    }
    try {
      setResult(await caoFetch("GET", `/sessions/${encodeURIComponent(name)}`));
    } catch (e) {
      setResult({ ok: false, status: 0, body: String(e), duration: 0 });
    }
  }, [name]);
  return (
    <div>
      <p style={{ fontSize: 13, color: "var(--text-dim)", marginBottom: 16 }}>
        Retrieve details of a specific session by name.
      </p>
      <CardWrap method="GET" path="/sessions/{session_name}" desc="Session details">
        <Field label="session_name" required>
          <input
            style={inputStyle}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. my-session"
          />
        </Field>
        <Btn onClick={run}>Send Request</Btn>
        <ResultBox result={result} />
      </CardWrap>
    </div>
  );
}

function DeleteSessionPanel() {
  const [name, setName] = useState("");
  const [result, setResult] = useState<ApiResult | null>(null);
  const run = useCallback(async () => {
    if (!name.trim()) {
      alert("session_name is required");
      return;
    }
    if (!confirm(`Delete session "${name}"?`)) return;
    try {
      setResult(
        await caoFetch("DELETE", `/sessions/${encodeURIComponent(name)}`)
      );
    } catch (e) {
      setResult({ ok: false, status: 0, body: String(e), duration: 0 });
    }
  }, [name]);
  return (
    <div>
      <p style={{ fontSize: 13, color: "var(--text-dim)", marginBottom: 16 }}>
        Permanently delete a session and all its terminals.
      </p>
      <CardWrap method="DELETE" path="/sessions/{session_name}" desc="Remove session">
        <Field label="session_name" required>
          <input
            style={inputStyle}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. my-session"
          />
        </Field>
        <Btn danger onClick={run}>
          Send Request
        </Btn>
        <ResultBox result={result} />
      </CardWrap>
    </div>
  );
}

function ListSessionTerminalsPanel() {
  const [name, setName] = useState("");
  const [result, setResult] = useState<ApiResult | null>(null);
  const run = useCallback(async () => {
    if (!name.trim()) {
      alert("session_name is required");
      return;
    }
    try {
      setResult(
        await caoFetch(
          "GET",
          `/sessions/${encodeURIComponent(name)}/terminals`
        )
      );
    } catch (e) {
      setResult({ ok: false, status: 0, body: String(e), duration: 0 });
    }
  }, [name]);
  return (
    <div>
      <p style={{ fontSize: 13, color: "var(--text-dim)", marginBottom: 16 }}>
        List all terminals that belong to a session.
      </p>
      <CardWrap
        method="GET"
        path="/sessions/{session_name}/terminals"
        desc="Session terminals"
      >
        <Field label="session_name" required>
          <input
            style={inputStyle}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. my-session"
          />
        </Field>
        <Btn onClick={run}>Send Request</Btn>
        <ResultBox result={result} />
      </CardWrap>
    </div>
  );
}

function CreateSessionTerminalPanel() {
  const [sessionName, setSessionName] = useState("");
  const [agentProfile, setAgentProfile] = useState("");
  const [provider, setProvider] = useState("");
  const [workingDir, setWorkingDir] = useState("");
  const [result, setResult] = useState<ApiResult | null>(null);

  const run = useCallback(async () => {
    if (!sessionName.trim() || !agentProfile.trim()) {
      alert("session_name and agent_profile are required");
      return;
    }
    try {
      setResult(
        await caoFetch(
          "POST",
          `/sessions/${encodeURIComponent(sessionName)}/terminals`,
          { agent_profile: agentProfile, provider, working_directory: workingDir }
        )
      );
    } catch (e) {
      setResult({ ok: false, status: 0, body: String(e), duration: 0 });
    }
  }, [sessionName, agentProfile, provider, workingDir]);

  return (
    <div>
      <p style={{ fontSize: 13, color: "var(--text-dim)", marginBottom: 16 }}>
        Add an additional terminal (agent window) to an existing session.
      </p>
      <CardWrap
        method="POST"
        path="/sessions/{session_name}/terminals"
        desc="Add terminal"
      >
        <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
          <div style={{ flex: 1, minWidth: 160 }}>
            <Field label="session_name" required>
              <input
                style={inputStyle}
                value={sessionName}
                onChange={(e) => setSessionName(e.target.value)}
                placeholder="e.g. my-session"
              />
            </Field>
          </div>
          <div style={{ flex: 1, minWidth: 160 }}>
            <Field label="agent_profile" required>
              <input
                style={inputStyle}
                value={agentProfile}
                onChange={(e) => setAgentProfile(e.target.value)}
                placeholder="e.g. coding-agent"
              />
            </Field>
          </div>
        </div>
        <Field label="provider (optional)">
          <select
            style={selectStyle}
            value={provider}
            onChange={(e) => setProvider(e.target.value)}
          >
            <option value="">— default (kiro_cli) —</option>
            <option value="kiro_cli">kiro_cli</option>
            <option value="q_cli">q_cli</option>
            <option value="claude_code">claude_code</option>
            <option value="codex">codex</option>
            <option value="qoder_cli">qoder_cli</option>
            <option value="opencode">opencode</option>
            <option value="codebuddy">codebuddy</option>
            <option value="copilot">copilot</option>
          </select>
        </Field>
        <Field label="working_directory (optional)">
          <input
            style={inputStyle}
            value={workingDir}
            onChange={(e) => setWorkingDir(e.target.value)}
            placeholder="e.g. /home/user/project"
          />
        </Field>
        <Btn onClick={run}>Send Request</Btn>
        <ResultBox result={result} />
      </CardWrap>
    </div>
  );
}

function GetTerminalPanel() {
  const [id, setId] = useState("");
  const [result, setResult] = useState<ApiResult | null>(null);
  const run = useCallback(async () => {
    if (!id.trim()) {
      alert("terminal_id is required");
      return;
    }
    try {
      setResult(await caoFetch("GET", `/terminals/${encodeURIComponent(id)}`));
    } catch (e) {
      setResult({ ok: false, status: 0, body: String(e), duration: 0 });
    }
  }, [id]);
  return (
    <div>
      <p style={{ fontSize: 13, color: "var(--text-dim)", marginBottom: 16 }}>
        Retrieve details of a terminal by its 8-character hex ID.
      </p>
      <CardWrap method="GET" path="/terminals/{terminal_id}" desc="Terminal details">
        <Field label="terminal_id (8-char hex)" required>
          <input
            style={inputStyle}
            value={id}
            onChange={(e) => setId(e.target.value)}
            placeholder="e.g. a1b2c3d4"
            maxLength={8}
          />
        </Field>
        <Btn onClick={run}>Send Request</Btn>
        <ResultBox result={result} />
      </CardWrap>
    </div>
  );
}

function WorkingDirPanel() {
  const [id, setId] = useState("");
  const [result, setResult] = useState<ApiResult | null>(null);
  const run = useCallback(async () => {
    if (!id.trim()) {
      alert("terminal_id is required");
      return;
    }
    try {
      setResult(
        await caoFetch(
          "GET",
          `/terminals/${encodeURIComponent(id)}/working-directory`
        )
      );
    } catch (e) {
      setResult({ ok: false, status: 0, body: String(e), duration: 0 });
    }
  }, [id]);
  return (
    <div>
      <p style={{ fontSize: 13, color: "var(--text-dim)", marginBottom: 16 }}>
        Retrieve the current working directory of a terminal&#39;s pane.
      </p>
      <CardWrap
        method="GET"
        path="/terminals/{terminal_id}/working-directory"
        desc="Terminal working dir"
      >
        <Field label="terminal_id (8-char hex)" required>
          <input
            style={inputStyle}
            value={id}
            onChange={(e) => setId(e.target.value)}
            placeholder="e.g. a1b2c3d4"
            maxLength={8}
          />
        </Field>
        <Btn onClick={run}>Send Request</Btn>
        <ResultBox result={result} />
      </CardWrap>
    </div>
  );
}

function SendInputPanel() {
  const [id, setId] = useState("");
  const [message, setMessage] = useState("");
  const [result, setResult] = useState<ApiResult | null>(null);
  const run = useCallback(async () => {
    if (!id.trim() || !message) {
      alert("terminal_id and message are required");
      return;
    }
    try {
      setResult(
        await caoFetch(
          "POST",
          `/terminals/${encodeURIComponent(id)}/input`,
          { message }
        )
      );
    } catch (e) {
      setResult({ ok: false, status: 0, body: String(e), duration: 0 });
    }
  }, [id, message]);
  return (
    <div>
      <p style={{ fontSize: 13, color: "var(--text-dim)", marginBottom: 16 }}>
        Send a message or command to a running terminal.
      </p>
      <CardWrap
        method="POST"
        path="/terminals/{terminal_id}/input"
        desc="Send input text"
      >
        <Field label="terminal_id (8-char hex)" required>
          <input
            style={inputStyle}
            value={id}
            onChange={(e) => setId(e.target.value)}
            placeholder="e.g. a1b2c3d4"
            maxLength={8}
          />
        </Field>
        <Field label="message" required>
          <textarea
            style={textareaStyle}
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            placeholder="Enter the message to send to the agent..."
          />
        </Field>
        <Btn onClick={run}>Send Request</Btn>
        <ResultBox result={result} />
      </CardWrap>
    </div>
  );
}

function GetOutputPanel() {
  const [id, setId] = useState("");
  const [mode, setMode] = useState("full");
  const [result, setResult] = useState<ApiResult | null>(null);
  const run = useCallback(async () => {
    if (!id.trim()) {
      alert("terminal_id is required");
      return;
    }
    try {
      setResult(
        await caoFetch(
          "GET",
          `/terminals/${encodeURIComponent(id)}/output`,
          { mode }
        )
      );
    } catch (e) {
      setResult({ ok: false, status: 0, body: String(e), duration: 0 });
    }
  }, [id, mode]);
  return (
    <div>
      <p style={{ fontSize: 13, color: "var(--text-dim)", marginBottom: 16 }}>
        Read the terminal&#39;s captured output log.
      </p>
      <CardWrap
        method="GET"
        path="/terminals/{terminal_id}/output"
        desc="Terminal output log"
      >
        <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
          <div style={{ flex: 1, minWidth: 160 }}>
            <Field label="terminal_id (8-char hex)" required>
              <input
                style={inputStyle}
                value={id}
                onChange={(e) => setId(e.target.value)}
                placeholder="e.g. a1b2c3d4"
                maxLength={8}
              />
            </Field>
          </div>
          <div style={{ flex: 1, minWidth: 120 }}>
            <Field label="mode (optional)">
              <select
                style={selectStyle}
                value={mode}
                onChange={(e) => setMode(e.target.value)}
              >
                <option value="full">full (default)</option>
                <option value="last">last</option>
                <option value="tail">tail</option>
              </select>
            </Field>
          </div>
        </div>
        <Btn onClick={run}>Send Request</Btn>
        <ResultBox result={result} />
      </CardWrap>
    </div>
  );
}

function ExitTerminalPanel() {
  const [id, setId] = useState("");
  const [result, setResult] = useState<ApiResult | null>(null);
  const run = useCallback(async () => {
    if (!id.trim()) {
      alert("terminal_id is required");
      return;
    }
    try {
      setResult(
        await caoFetch("POST", `/terminals/${encodeURIComponent(id)}/exit`)
      );
    } catch (e) {
      setResult({ ok: false, status: 0, body: String(e), duration: 0 });
    }
  }, [id]);
  return (
    <div>
      <p style={{ fontSize: 13, color: "var(--text-dim)", marginBottom: 16 }}>
        Send a provider-specific exit command to gracefully stop the agent.
      </p>
      <CardWrap
        method="POST"
        path="/terminals/{terminal_id}/exit"
        desc="Send exit command"
      >
        <Field label="terminal_id (8-char hex)" required>
          <input
            style={inputStyle}
            value={id}
            onChange={(e) => setId(e.target.value)}
            placeholder="e.g. a1b2c3d4"
            maxLength={8}
          />
        </Field>
        <Btn onClick={run}>Send Request</Btn>
        <ResultBox result={result} />
      </CardWrap>
    </div>
  );
}

function DeleteTerminalPanel() {
  const [id, setId] = useState("");
  const [result, setResult] = useState<ApiResult | null>(null);
  const run = useCallback(async () => {
    if (!id.trim()) {
      alert("terminal_id is required");
      return;
    }
    if (!confirm(`Delete terminal "${id}"?`)) return;
    try {
      setResult(
        await caoFetch("DELETE", `/terminals/${encodeURIComponent(id)}`)
      );
    } catch (e) {
      setResult({ ok: false, status: 0, body: String(e), duration: 0 });
    }
  }, [id]);
  return (
    <div>
      <p style={{ fontSize: 13, color: "var(--text-dim)", marginBottom: 16 }}>
        Permanently remove a terminal from the session.
      </p>
      <CardWrap
        method="DELETE"
        path="/terminals/{terminal_id}"
        desc="Remove terminal"
      >
        <Field label="terminal_id (8-char hex)" required>
          <input
            style={inputStyle}
            value={id}
            onChange={(e) => setId(e.target.value)}
            placeholder="e.g. a1b2c3d4"
            maxLength={8}
          />
        </Field>
        <Btn danger onClick={run}>
          Send Request
        </Btn>
        <ResultBox result={result} />
      </CardWrap>
    </div>
  );
}

function SendInboxPanel() {
  const [receiverId, setReceiverId] = useState("");
  const [senderId, setSenderId] = useState("");
  const [message, setMessage] = useState("");
  const [result, setResult] = useState<ApiResult | null>(null);

  const run = useCallback(async () => {
    if (!receiverId.trim() || !senderId.trim() || !message) {
      alert("receiver_id, sender_id and message are required");
      return;
    }
    try {
      setResult(
        await caoFetch(
          "POST",
          `/terminals/${encodeURIComponent(receiverId)}/inbox/messages`,
          { sender_id: senderId, message }
        )
      );
    } catch (e) {
      setResult({ ok: false, status: 0, body: String(e), duration: 0 });
    }
  }, [receiverId, senderId, message]);

  return (
    <div>
      <p style={{ fontSize: 13, color: "var(--text-dim)", marginBottom: 16 }}>
        Queue a message from one terminal to another. Delivered when the
        receiver is idle.
      </p>
      <CardWrap
        method="POST"
        path="/terminals/{receiver_id}/inbox/messages"
        desc="Send inbox message"
      >
        <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
          <div style={{ flex: 1, minWidth: 160 }}>
            <Field label="receiver_id (8-char hex)" required>
              <input
                style={inputStyle}
                value={receiverId}
                onChange={(e) => setReceiverId(e.target.value)}
                placeholder="e.g. a1b2c3d4"
                maxLength={8}
              />
            </Field>
          </div>
          <div style={{ flex: 1, minWidth: 160 }}>
            <Field label="sender_id (8-char hex)" required>
              <input
                style={inputStyle}
                value={senderId}
                onChange={(e) => setSenderId(e.target.value)}
                placeholder="e.g. b2c3d4e5"
                maxLength={8}
              />
            </Field>
          </div>
        </div>
        <Field label="message" required>
          <textarea
            style={textareaStyle}
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            placeholder="Message content to deliver to the receiver terminal..."
          />
        </Field>
        <Btn onClick={run}>Send Request</Btn>
        <ResultBox result={result} />
      </CardWrap>
    </div>
  );
}

function GetInboxPanel() {
  const [id, setId] = useState("");
  const [limit, setLimit] = useState("10");
  const [status, setStatus] = useState("");
  const [result, setResult] = useState<ApiResult | null>(null);

  const run = useCallback(async () => {
    if (!id.trim()) {
      alert("terminal_id is required");
      return;
    }
    try {
      setResult(
        await caoFetch(
          "GET",
          `/terminals/${encodeURIComponent(id)}/inbox/messages`,
          { limit, status }
        )
      );
    } catch (e) {
      setResult({ ok: false, status: 0, body: String(e), duration: 0 });
    }
  }, [id, limit, status]);

  return (
    <div>
      <p style={{ fontSize: 13, color: "var(--text-dim)", marginBottom: 16 }}>
        Retrieve queued or delivered inbox messages for a terminal.
      </p>
      <CardWrap
        method="GET"
        path="/terminals/{terminal_id}/inbox/messages"
        desc="List inbox messages"
      >
        <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
          <div style={{ flex: 2, minWidth: 160 }}>
            <Field label="terminal_id (8-char hex)" required>
              <input
                style={inputStyle}
                value={id}
                onChange={(e) => setId(e.target.value)}
                placeholder="e.g. a1b2c3d4"
                maxLength={8}
              />
            </Field>
          </div>
          <div style={{ flex: 1, minWidth: 80 }}>
            <Field label="limit (max 100)">
              <input
                style={inputStyle}
                type="number"
                value={limit}
                onChange={(e) => setLimit(e.target.value)}
                min={1}
                max={100}
              />
            </Field>
          </div>
          <div style={{ flex: 1, minWidth: 120 }}>
            <Field label="status (optional)">
              <select
                style={selectStyle}
                value={status}
                onChange={(e) => setStatus(e.target.value)}
              >
                <option value="">— all statuses —</option>
                <option value="pending">pending</option>
                <option value="delivered">delivered</option>
                <option value="failed">failed</option>
              </select>
            </Field>
          </div>
        </div>
        <Btn onClick={run}>Send Request</Btn>
        <ResultBox result={result} />
      </CardWrap>
    </div>
  );
}

// ─── Navigation config ────────────────────────────────────────────────────────

type NavItem = {
  id: PanelId;
  label: string;
  badge?: "GET" | "POST" | "DEL";
};
type NavGroup = { group: string; items: NavItem[] };

const NAV: NavGroup[] = [
  {
    group: "Server",
    items: [{ id: "health", label: "Health Check", badge: "GET" }],
  },
  {
    group: "Sessions",
    items: [
      { id: "sessions-list", label: "List Sessions", badge: "GET" },
      { id: "sessions-create", label: "Create Session", badge: "POST" },
      { id: "sessions-get", label: "Get Session", badge: "GET" },
      { id: "sessions-delete", label: "Delete Session", badge: "DEL" },
    ],
  },
  {
    group: "Session Terminals",
    items: [
      {
        id: "session-terminals-list",
        label: "List Terminals",
        badge: "GET",
      },
      {
        id: "session-terminals-create",
        label: "Create Terminal",
        badge: "POST",
      },
    ],
  },
  {
    group: "Terminals",
    items: [
      { id: "terminals-get", label: "Get Terminal", badge: "GET" },
      { id: "terminals-workdir", label: "Working Directory", badge: "GET" },
      { id: "terminals-input", label: "Send Input", badge: "POST" },
      { id: "terminals-output", label: "Get Output", badge: "GET" },
      { id: "terminals-exit", label: "Exit Terminal", badge: "POST" },
      { id: "terminals-delete", label: "Delete Terminal", badge: "DEL" },
    ],
  },
  {
    group: "Inbox",
    items: [
      { id: "inbox-send", label: "Send Message", badge: "POST" },
      { id: "inbox-list", label: "Get Messages", badge: "GET" },
    ],
  },
];

const BADGE_COLORS: Record<string, { bg: string; color: string }> = {
  GET: { bg: "#1b3a6a", color: "#4f8ef7" },
  POST: { bg: "#1b4a2a", color: "#3dbe7a" },
  DEL: { bg: "#4a1b1b", color: "#e05252" },
};

// ─── Page component ───────────────────────────────────────────────────────────

const PANELS: Record<PanelId, React.ComponentType> = {
  health: HealthPanel,
  "sessions-list": ListSessionsPanel,
  "sessions-create": CreateSessionPanel,
  "sessions-get": GetSessionPanel,
  "sessions-delete": DeleteSessionPanel,
  "session-terminals-list": ListSessionTerminalsPanel,
  "session-terminals-create": CreateSessionTerminalPanel,
  "terminals-get": GetTerminalPanel,
  "terminals-workdir": WorkingDirPanel,
  "terminals-input": SendInputPanel,
  "terminals-output": GetOutputPanel,
  "terminals-exit": ExitTerminalPanel,
  "terminals-delete": DeleteTerminalPanel,
  "inbox-send": SendInboxPanel,
  "inbox-list": GetInboxPanel,
};

const PANEL_TITLES: Record<PanelId, string> = {
  health: "Health Check",
  "sessions-list": "List Sessions",
  "sessions-create": "Create Session",
  "sessions-get": "Get Session",
  "sessions-delete": "Delete Session",
  "session-terminals-list": "List Session Terminals",
  "session-terminals-create": "Create Session Terminal",
  "terminals-get": "Get Terminal",
  "terminals-workdir": "Working Directory",
  "terminals-input": "Send Input",
  "terminals-output": "Get Output",
  "terminals-exit": "Exit Terminal",
  "terminals-delete": "Delete Terminal",
  "inbox-send": "Send Inbox Message",
  "inbox-list": "Get Inbox Messages",
};

export default function ConsolePage() {
  const [active, setActive] = useState<PanelId>("health");
  const ActivePanel = PANELS[active];

  return (
    <div
      style={{
        display: "flex",
        height: "100vh",
        fontFamily: "var(--font)",
        background: "var(--bg)",
        color: "var(--text)",
      }}
    >
      {/* ── Sidebar ── */}
      <aside
        style={{
          width: 220,
          minWidth: 220,
          background: "var(--surface)",
          borderRight: "1px solid var(--border)",
          display: "flex",
          flexDirection: "column",
          overflowY: "auto",
        }}
      >
        <div
          style={{
            padding: "18px 16px 12px",
            borderBottom: "1px solid var(--border)",
            fontSize: 13,
            fontWeight: 700,
            color: "var(--accent)",
            letterSpacing: ".5px",
            lineHeight: 1.4,
          }}
        >
          CAO
          <small
            style={{
              display: "block",
              fontWeight: 400,
              color: "var(--text-dim)",
              fontSize: 11,
            }}
          >
            CLI Agent Orchestrator
          </small>
        </div>

        <nav style={{ padding: "8px 0", flex: 1 }}>
          {NAV.map((g) => (
            <React.Fragment key={g.group}>
              <div
                style={{
                  padding: "12px 16px 4px",
                  fontSize: 10,
                  fontWeight: 600,
                  color: "var(--text-dim)",
                  textTransform: "uppercase",
                  letterSpacing: 1,
                }}
              >
                {g.group}
              </div>
              {g.items.map((item) => {
                const bc = item.badge ? BADGE_COLORS[item.badge] : null;
                const isActive = active === item.id;
                return (
                  <div
                    key={item.id}
                    onClick={() => setActive(item.id)}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 8,
                      padding: "8px 16px",
                      cursor: "pointer",
                      color: isActive ? "var(--accent)" : "var(--text)",
                      fontSize: 13,
                      borderLeft: isActive
                        ? "3px solid var(--accent)"
                        : "3px solid transparent",
                      background: isActive ? "var(--surface2)" : "transparent",
                      transition: "background .15s, color .15s",
                    }}
                  >
                    {item.label}
                    {bc && (
                      <span
                        style={{
                          marginLeft: "auto",
                          fontSize: 10,
                          padding: "1px 6px",
                          borderRadius: 9,
                          fontWeight: 600,
                          background: bc.bg,
                          color: bc.color,
                        }}
                      >
                        {item.badge}
                      </span>
                    )}
                  </div>
                );
              })}
            </React.Fragment>
          ))}
        </nav>
      </aside>

      {/* ── Main ── */}
      <main
        style={{
          flex: 1,
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
        }}
      >
        {/* Topbar */}
        <div
          style={{
            padding: "12px 24px",
            borderBottom: "1px solid var(--border)",
            background: "var(--surface)",
            display: "flex",
            alignItems: "center",
            gap: 12,
            flexShrink: 0,
          }}
        >
          <h1
            style={{
              fontSize: 16,
              fontWeight: 600,
              color: "var(--text-bright)",
              flex: 1,
            }}
          >
            {PANEL_TITLES[active]}
          </h1>
          <span
            style={{
              fontSize: 11,
              color: "var(--text-dim)",
              fontFamily: "var(--mono)",
            }}
          >
            cao-server → localhost:9889
          </span>
        </div>

        {/* Content */}
        <div
          style={{
            flex: 1,
            overflowY: "auto",
            padding: 24,
          }}
        >
          <ActivePanel />
        </div>
      </main>
    </div>
  );
}
