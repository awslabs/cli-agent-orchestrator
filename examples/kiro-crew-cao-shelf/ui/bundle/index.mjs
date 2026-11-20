import { jsxs, jsx, Fragment } from "react/jsx-runtime";
import * as sdk from "@kirocrew/app-sdk";
import { useAppApi, useAppInfo, useNavBadge } from "@kirocrew/app-sdk";
import * as ui from "@kirocrew/app-sdk/ui";
import { Card, CardTitle, ErrorNotice, Btn, Modal, Badge, Input, Toggle, EmptyState, PageHeader, Skeleton } from "@kirocrew/app-sdk/ui";
import { useMemo, createContext, useContext, useState, useEffect, forwardRef, useRef, useId, useCallback } from "react";
const BASE = "/apps/cao-shelf/api";
function toShelfError(e) {
  const err = e;
  if (typeof err?.status === "number") {
    try {
      const parsed = JSON.parse(err.body ?? "");
      if (parsed.error && typeof parsed.error.code === "string") {
        return {
          status: err.status,
          code: parsed.error.code,
          message: String(parsed.error.message ?? ""),
          retryable: !!parsed.error.retryable,
          detail: parsed.error.detail
        };
      }
    } catch {
    }
    return {
      status: err.status,
      code: "backend_unreachable",
      message: `The CAO Shelf backend answered ${err.status}.`,
      retryable: true
    };
  }
  if (err?.name === "AbortError") return { status: null, code: "backend_unreachable", message: "Request cancelled.", retryable: true };
  return {
    status: null,
    code: "backend_unreachable",
    message: "Cannot reach the CAO Shelf backend.",
    retryable: true
  };
}
const enc = encodeURIComponent;
function makeApi(api) {
  return {
    shelf: (since, signal) => api.get(`${BASE}/shelf${since === null ? "" : `?since=${since}`}`, { signal }),
    history: (name) => api.get(`${BASE}/sessions/${enc(name)}/history`),
    historyRows: (name, terminalId) => api.get(`${BASE}/sessions/${enc(name)}/history/${enc(terminalId)}/rows`),
    allHistory: () => api.get(`${BASE}/history`),
    prompt: (id) => api.get(`${BASE}/terminals/${enc(id)}/prompt`),
    ask: (name, body) => api.post(`${BASE}/sessions/${enc(name)}/ask`, body),
    answer: (id, body) => api.post(`${BASE}/terminals/${enc(id)}/answer`, body),
    profiles: () => api.get(`${BASE}/profiles`),
    start: (body) => api.post(`${BASE}/sessions`, body),
    stopPlan: (body) => api.post(`${BASE}/stop/plan`, body),
    stopExecute: (body) => api.post(`${BASE}/stop/execute`, body),
    getConfig: () => api.get(`${BASE}/config`),
    putConfig: (body) => api.put(`${BASE}/config`, body),
    relays: () => api.get(`${BASE}/relays`),
    relayCreate: (body) => api.post(`${BASE}/relays`, body),
    relayEnd: (id, body) => api.post(`${BASE}/relays/${enc(id)}/end`, body),
    relayTakeover: (id, body) => api.post(`${BASE}/relays/${enc(id)}/takeover`, body)
  };
}
function useShelfApi() {
  const api = useAppApi();
  return useMemo(() => makeApi(api), [api]);
}
const ShelfContext = createContext(null);
function useShelfCtx() {
  const c = useContext(ShelfContext);
  if (!c) throw new Error("ShelfContext missing");
  return c;
}
function fmtAge(seconds) {
  const s = Math.max(0, seconds);
  if (s < 60) return "<1 min";
  if (s < 3600) return `${Math.round(s / 60)} min`;
  if (s < 48 * 3600) return `${Math.round(s / 3600)} h`;
  return `${Math.round(s / 86400)} d`;
}
function ageSince(iso, now) {
  const a = Date.parse(iso);
  const b = typeof now === "number" ? now : Date.parse(now);
  if (Number.isNaN(a) || Number.isNaN(b)) return 0;
  return Math.max(0, Math.floor((b - a) / 1e3));
}
function ageWords(iso, now) {
  return Number.isNaN(Date.parse(iso)) ? "age unknown" : fmtAge(ageSince(iso, now));
}
const hours = (h) => Number.isInteger(h) ? String(h) : String(Math.round(h * 100) / 100);
function helperName(label, id) {
  return label ?? `helper ${id.slice(0, 4)}`;
}
function AllHistory() {
  const { api, now } = useShelfCtx();
  const [d, setD] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    let dead = false;
    api.allHistory().then((r) => !dead && setD(r)).catch((e) => !dead && setError(toShelfError(e).message || "History could not be read."));
    return () => {
      dead = true;
    };
  }, [api]);
  const sorted = d ? [...d.closed].sort((a, b) => Date.parse(b.last_at) - Date.parse(a.last_at)) : [];
  return /* @__PURE__ */ jsxs(Card, { className: "cs-box", role: "region", "aria-label": "All history", children: [
    /* @__PURE__ */ jsxs(CardTitle, { className: "cs-box-title", children: [
      "Closed helpers, newest first ",
      /* @__PURE__ */ jsx("small", { className: "cs-muted", children: "(from inbox rows; CAO deleted the terminals)" })
    ] }),
    /* @__PURE__ */ jsx(ErrorNotice, { className: "cs-gap", message: error }),
    !d && !error ? /* @__PURE__ */ jsx("p", { className: "cs-muted", children: "Loading..." }) : null,
    d ? /* @__PURE__ */ jsxs(Fragment, { children: [
      /* @__PURE__ */ jsx("ul", { children: sorted.slice(0, 8).map((c) => /* @__PURE__ */ jsxs("li", { children: [
        /* @__PURE__ */ jsx("b", { children: helperName(null, c.terminal_id) }),
        " in ",
        c.session,
        ", last message ",
        fmtAge(ageSince(c.last_at, now)),
        " ago (",
        c.rows,
        " ",
        c.rows === 1 ? "row" : "rows",
        ")"
      ] }, `${c.session}/${c.terminal_id}`)) }),
      sorted.length > 8 ? /* @__PURE__ */ jsxs("small", { className: "cs-muted", children: [
        "and ",
        sorted.length - 8,
        " older"
      ] }) : null,
      sorted.length === 0 ? /* @__PURE__ */ jsx("p", { className: "cs-muted", children: "none recorded" }) : null,
      !d.complete ? /* @__PURE__ */ jsx("p", { className: "cs-muted", children: "Some sessions were not read yet or their window was full, so older closed helpers may be missing." }) : null
    ] }) : null
  ] });
}
const caution = ui.guideCaution ?? {};
function Select(props) {
  const Host = ui.Select;
  if (Host) return /* @__PURE__ */ jsx(Host, { ...props });
  const { options, optionLabels, value, onChange, clearLabel, disabled, title, className } = props;
  return /* @__PURE__ */ jsxs("select", { "aria-label": props["aria-label"], title, disabled, value, onChange: (e) => onChange(e.target.value), className, children: [
    clearLabel !== void 0 ? /* @__PURE__ */ jsx("option", { value: "", children: clearLabel }) : null,
    options.map((o, i) => /* @__PURE__ */ jsx("option", { value: o, children: optionLabels?.[i] ?? o }, o))
  ] });
}
const Button = forwardRef(function Button2({ type = "button", className, quiet, danger, ...rest }, ref) {
  const cls = ["cs-btn", quiet ? "cs-quiet" : "", className ?? ""].filter(Boolean).join(" ");
  return /* @__PURE__ */ jsx(Btn, { ref, type, danger, className: cls, ...danger ? caution : {}, ...rest });
});
function useOpenChat() {
  const hook = sdk.useChatLauncher;
  const launcher = hook ? hook() : null;
  return launcher ? (slotKey) => launcher.openChat({ slotKey }) : null;
}
const RELAY_EXPLAIN = "Remote control ties a Kiro Crew chat to one CAO session, so you can talk to that session from the chat.";
const RELAY_EXPLAIN_SUMMARY = "What is remote control?";
const RELAY_EXPLAIN_DETAIL = [
  "Your messages go to the session's conductor tagged [REMOTE-CONTROL], and its replies come back to the chat. A message that starts with / goes to the session untagged.",
  "The chat's relay agent reads the conductor's replies to judge progress. Idle does not mean done: the conductor can be idle while its helpers are still working. So the agent checks back with pauses between checks, and reports what the conductor said and which helpers are still working.",
  "You can keep typing to the session directly. Your next message from the chat then carries the tag [REMOTE-CONTROL catch-up], and the session opens its reply with a brief of what happened in between.",
  "End relay stops the chat, never the CAO session: the session keeps working.",
  "To start, press Remote control on a session's card, or tick and remote control it when you start a session."
];
const RELAY_RISK = "A Kiro Crew chat sends your messages to the CAO session and shows its replies. Text from the session reaches the chat's model, so an instruction hidden in it could make the model call a tool the chat has, and an allowed tool runs without asking you. On the claude backend the chat also has your own Claude Code tools and plugins, and on any backend a host plugin can add servers to every agent, for example a credentials server.";
const RELAY_RISK_DETAIL = [
  "On the kas backend the chat is offered only its four CAO tools (send_session_message, get_terminal_status, read_session_output and get_session_info) and Kiro Crew's wait tool, which only pauses the chat. They run without asking you, and a CAO tool runs for any terminal the model names. No Kiro built-in tool (shell, files, web or sub-agents), no Claude Code setting or plugin and no server from your mcp.json reaches it, except servers that Kiro Crew or a host plugin adds to every agent.",
  "On the claude backend the chat runs with your own Claude Code settings. Besides its four CAO tools and Kiro Crew's wait tool (which only pauses the chat), it can reach your Claude Code plugins, your user-level MCP servers and Claude Code's other built-in tools (for example Read, WebFetch, sub-agents, scheduled tasks and worktrees). Kiro Crew refuses the shell and Claude Code's file-edit tools here; if you enable the Claude Code sandbox, also set sandbox.autoAllowBashIfSandboxed to false, or sandboxed shell commands run without that refusal. A call runs without asking you when your Claude Code settings allow that tool or use auto mode. Sub-agents start without asking and can go on after End relay or Take over. Read plus WebFetch, or any plugin that posts or sends, is enough to send out any file you can read.",
  "On both backends a host plugin can add an MCP server to every agent, this one included: on some installs a credentials server whose tool hands out cloud credentials. Such a tool asks you first unless an allow rule, auto mode or a YOLO grant covers it. In a relay chat, refuse any request for a tool other than the four CAO tools and wait."
];
const SENTENCES = {
  no_conductor: "This session has no conductor, so there is nothing to remote control.",
  relay_exists: "This session already has an active relay.",
  risk_not_acknowledged: "Remote control was not set up: accept the stated risk first.",
  relay_cao_mismatch: "The remote-control agent was installed for another CAO address. Run install.sh again with --cao-url set to this page's CAO address.",
  gateway_unavailable: "CAO Shelf could not reach Kiro Crew to set up the relay chat. Try again in a moment.",
  gateway_refused: "Kiro Crew refused to create the relay chat.",
  not_found: "That session or relay is gone. Wait for the next refresh.",
  conflict_changed: "That relay is no longer active. Press Remote control to start a new one.",
  cao_unreachable: "CAO is not reachable, so the session could not be checked.",
  cao_timeout: "CAO did not answer in time. Try again.",
  invalid_request: "The request was refused as invalid.",
  backend_unreachable: "Cannot reach the CAO Shelf backend."
};
function relayErrorText(e) {
  const gw = e.detail?.gateway_code;
  const code = typeof gw === "string" && /^[a-z0-9_]{1,64}$/.test(gw) ? `${e.code} (${gw})` : e.code;
  const sentence = SENTENCES[e.code];
  if (sentence) return `${code}: ${sentence}`;
  return `${code}: ${e.message || "The relay was not created."}`;
}
const NO_CONDUCTOR = "This session has no conductor, so there is nothing to remote control.";
const NOT_TOLD = "Kiro Crew did not answer, so the chat has not been told the relay ended yet: it keeps relaying until it is. CAO Shelf tells it as soon as Kiro Crew answers.";
const PENDING_SHORT = "A tool request is waiting in the relay chat. Deny it unless you expect it.";
const PENDING_NOTICE = `A tool request is waiting in the relay chat. The relay's four CAO tools and its wait tool, which only pauses the chat, never ask on the kas backend, nor on claude with the allow rule from the README section "Remote control", so deny this request unless you are on claude, have not added that rule yet, and the chat shows one of those five tools. The name a request shows can be chosen by the model.`;
function RemoteControlIntro() {
  return /* @__PURE__ */ jsxs("div", { className: "cs-rc-intro", role: "note", "aria-label": "About remote control", children: [
    /* @__PURE__ */ jsx("p", { children: RELAY_EXPLAIN }),
    /* @__PURE__ */ jsxs("details", { className: "cs-disc cs-disc--bare", children: [
      /* @__PURE__ */ jsx("summary", { children: RELAY_EXPLAIN_SUMMARY }),
      RELAY_EXPLAIN_DETAIL.map((p) => /* @__PURE__ */ jsx("p", { children: p }, p.slice(0, 24)))
    ] })
  ] });
}
function RelayControls({ s }) {
  const { api, announce, refresh } = useShelfCtx();
  const openChat = useOpenChat();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [fallback, setFallback] = useState(null);
  const relay = s.relay;
  const open = (slotKey, title) => {
    setFallback(null);
    if (openChat) {
      openChat(slotKey);
      announce(`Remote control chat for ${s.name} is open`);
    } else {
      setFallback(title);
      announce(`Open the chat "${title}" from the Kiro Crew sidebar.`);
    }
  };
  const end = async (id) => {
    setBusy(true);
    setError(null);
    setFallback(null);
    try {
      const r = await api.relayEnd(id, {});
      announce(r.chat === "unreachable" ? `Relay ended. The CAO session keeps working. ${NOT_TOLD}` : "Relay ended. The CAO session keeps working.");
      refresh();
    } catch (e) {
      setError(relayErrorText(toShelfError(e)));
    } finally {
      setBusy(false);
    }
  };
  const active = relay && relay.state === "active" ? relay : null;
  if (!relay && !error && !fallback) return null;
  return /* @__PURE__ */ jsxs("div", { className: "cs-relay", children: [
    active ? /* @__PURE__ */ jsxs("div", { className: "cs-relay-on", role: "group", "aria-label": `relay of ${s.name}`, children: [
      /* @__PURE__ */ jsxs("span", { className: "cs-relay-ttl", children: [
        "Remote controlled from chat ",
        /* @__PURE__ */ jsx("b", { children: active.label })
      ] }),
      active.not_relayed > 0 ? /* @__PURE__ */ jsxs("small", { className: "cs-muted", title: "Messages the relay agent answered without sending them to the session or checking it", children: [
        active.not_relayed,
        " not relayed"
      ] }) : null,
      /* @__PURE__ */ jsxs("div", { className: "cs-row", children: [
        /* @__PURE__ */ jsx(Button, { onClick: () => open(active.slot_key, active.title), children: "Open chat" }),
        /* @__PURE__ */ jsx(Button, { danger: true, onClick: () => end(active.relay_id), busy, children: "End relay" })
      ] })
    ] }) : null,
    relay && relay.state === "needs_reattach" ? /* @__PURE__ */ jsxs("div", { className: "cs-relay-on cs-relay-off", children: [
      /* @__PURE__ */ jsx("span", { children: "Remote control stopped: this session's conductor changed." }),
      /* @__PURE__ */ jsx("div", { className: "cs-row", children: /* @__PURE__ */ jsx(Button, { onClick: () => end(relay.relay_id), busy, children: "Dismiss" }) })
    ] }) : null,
    active && active.permission_pending ? (
      // a caution to act on, not an error: amber, with the reason behind a disclosure
      /* @__PURE__ */ jsxs("div", { className: "cs-notice", "data-tone": "warn", children: [
        /* @__PURE__ */ jsx("span", { children: PENDING_SHORT }),
        /* @__PURE__ */ jsxs("details", { className: "cs-disc cs-disc--bare", children: [
          /* @__PURE__ */ jsx("summary", { children: "Why" }),
          /* @__PURE__ */ jsx("p", { children: PENDING_NOTICE })
        ] })
      ] })
    ) : null,
    /* @__PURE__ */ jsx(ErrorNotice, { message: error }),
    relay?.ended_notice_pending ? /* @__PURE__ */ jsx("p", { className: "cs-note", role: "status", children: NOT_TOLD }) : null,
    fallback ? /* @__PURE__ */ jsxs("p", { className: "cs-note", role: "status", children: [
      'Open the chat "',
      fallback,
      '" from the Kiro Crew sidebar.'
    ] }) : null
  ] });
}
function RiskText({ shortId }) {
  return /* @__PURE__ */ jsxs("div", { className: "cs-risk", children: [
    /* @__PURE__ */ jsx("p", { id: shortId, children: RELAY_RISK }),
    /* @__PURE__ */ jsxs("details", { className: "cs-disc", children: [
      /* @__PURE__ */ jsx("summary", { children: "What the chat can reach" }),
      RELAY_RISK_DETAIL.map((p) => /* @__PURE__ */ jsx("p", { children: p }, p.slice(0, 24)))
    ] })
  ] });
}
function RelayDialog({ s, onClose }) {
  const { api, announce, refresh } = useShelfCtx();
  const openChat = useOpenChat();
  const [existing, setExisting] = useState(
    () => s.relay && s.relay.state === "active" ? { relay_id: s.relay.relay_id, label: s.relay.label, slot_key: s.relay.slot_key, title: s.relay.title } : null
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [fallback, setFallback] = useState(null);
  const inFlight = useRef(false);
  const closeBtn = useRef(null);
  useEffect(() => {
    if (fallback) closeBtn.current?.focus();
  }, [fallback]);
  const opened = (slotKey, title) => {
    if (openChat) {
      openChat(slotKey);
      announce(`Remote control chat for ${s.name} is open`);
      onClose();
    } else {
      setFallback(title);
      announce(`Open the chat "${title}" from the Kiro Crew sidebar.`);
    }
  };
  const run = async (work) => {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      await work();
    } catch (e) {
      setError(relayErrorText(toShelfError(e)));
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  };
  const findSlot = async (relayId) => {
    try {
      const all = await api.relays();
      const r = all.relays.find((x) => x.relay_id === relayId);
      return r ? { relay_id: r.relay_id, label: r.label, slot_key: r.slot_key, title: r.title } : null;
    } catch {
      return null;
    }
  };
  const accept = () => run(async () => {
    try {
      const r = await api.relayCreate({ session: s.name, acknowledge_risk: true });
      refresh();
      opened(r.slot_key, r.title);
    } catch (e) {
      const err = toShelfError(e);
      const id = err.detail?.relay_id;
      if (err.code === "relay_exists" && typeof id === "string") {
        const label = typeof err.detail?.label === "string" ? err.detail.label : "";
        setExisting(await findSlot(id) ?? { relay_id: id, label, slot_key: null, title: null });
        refresh();
        return;
      }
      throw e;
    }
  });
  const takeOver = (id) => run(async () => {
    const r = await api.relayTakeover(id, { acknowledge_risk: true });
    refresh();
    opened(r.slot_key, r.title);
  });
  const footer = fallback ? /* @__PURE__ */ jsx("div", { className: "cs-actions", children: /* @__PURE__ */ jsx(Button, { primary: true, ref: closeBtn, onClick: onClose, children: "Done" }) }) : /* @__PURE__ */ jsxs("div", { className: "cs-actions", children: [
    /* @__PURE__ */ jsx(Button, { onClick: onClose, disabled: busy, children: "Cancel" }),
    existing?.slot_key ? /* @__PURE__ */ jsx(Button, { onClick: () => opened(existing.slot_key, existing.title ?? `Remote control: ${existing.label}`), disabled: busy, children: "Open chat" }) : null,
    existing ? /* @__PURE__ */ jsx(Button, { primary: true, onClick: () => takeOver(existing.relay_id), busy, children: busy ? "Taking over..." : "Take over" }) : /* @__PURE__ */ jsx(Button, { primary: true, onClick: accept, busy, children: busy ? "Setting up..." : "Accept and remote control" })
  ] });
  return /* @__PURE__ */ jsx(Modal, { open: true, onClose, title: /* @__PURE__ */ jsxs("span", { className: "cs-dlg-title", children: [
    "Remote control ",
    s.name,
    "?"
  ] }), maxWidth: 520, dismissDisabled: busy, footer, children: fallback ? /* @__PURE__ */ jsxs("p", { className: "cs-lead", role: "status", children: [
    'Open the chat "',
    fallback,
    '" from the Kiro Crew sidebar.'
  ] }) : /* @__PURE__ */ jsxs(Fragment, { children: [
    existing ? /* @__PURE__ */ jsxs("p", { className: "cs-lead", children: [
      "This session is already remote controlled from chat ",
      /* @__PURE__ */ jsx("b", { children: existing.label }),
      ". Take over ends that relay and makes a new relay chat."
    ] }) : null,
    /* @__PURE__ */ jsx(RiskText, {}),
    /* @__PURE__ */ jsx(ErrorNotice, { message: error })
  ] }) });
}
function Section({ title, count, empty, children }) {
  const id = useId();
  return /* @__PURE__ */ jsxs("section", { "aria-labelledby": id, children: [
    /* @__PURE__ */ jsxs("h2", { className: "cs-sec", id, children: [
      title,
      " (",
      count,
      ")"
    ] }),
    count === 0 ? empty ? /* @__PURE__ */ jsx("p", { className: "cs-none", children: empty }) : null : /* @__PURE__ */ jsx("div", { className: "cs-grid", children })
  ] });
}
function statusText(t, now) {
  const age = ageWords(t.last_active, now);
  const known = age !== "age unknown";
  switch (t.status) {
    case "idle":
      return known ? `idle ${age}` : "idle, age unknown";
    case "waiting_user_answer":
      return known ? `waiting about ${age}` : "waiting, age unknown";
    case "unknown":
      return known ? `status unknown, idle ${age}` : "status unknown, age unknown";
    default:
      return t.status;
  }
}
function Dot({ status }) {
  return /* @__PURE__ */ jsx("i", { className: `cs-dot cs-dot-${status}`, "aria-hidden": "true" });
}
function Chip({ t }) {
  const { now } = useShelfCtx();
  return /* @__PURE__ */ jsxs("li", { className: `cs-chip${t.orphan ? " cs-chip-orphan" : ""}`, children: [
    /* @__PURE__ */ jsx(Dot, { status: t.status }),
    /* @__PURE__ */ jsx("span", { children: t.label }),
    /* @__PURE__ */ jsx("small", { children: statusText(t, now) }),
    t.orphan ? /* @__PURE__ */ jsx(Badge, { variant: "err", children: "ORPHAN" }) : null,
    t.role === "peer" ? /* @__PURE__ */ jsx("small", { children: "(peer)" }) : null
  ] });
}
function AnswerBox({ ask }) {
  const { api, disabledReason, announce, refresh, now } = useShelfCtx();
  const id = useId();
  const [prompt, setPrompt] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [show, setShow] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState(null);
  const [gone, setGone] = useState(false);
  const timer = useRef(null);
  useEffect(() => {
    let dead = false;
    setLoadError(null);
    api.prompt(ask.terminal_id).then((p) => !dead && setPrompt(p)).catch((e) => !dead && setLoadError(toShelfError(e).message || "The prompt could not be read."));
    return () => {
      dead = true;
    };
  }, [api, ask.terminal_id, ask.since]);
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    []
  );
  const send = async (answer) => {
    if (!answer || busy || disabledReason) return;
    setBusy(true);
    setError(null);
    try {
      await api.answer(ask.terminal_id, { answer, expect_status: "waiting_user_answer" });
      setSent(true);
      setText("");
      announce(`Answer sent to ${ask.label}`);
      timer.current = setTimeout(() => setSent(false), 2e3);
      refresh();
    } catch (e) {
      const se = toShelfError(e);
      if (se.code === "not_waiting") {
        setGone(true);
        refresh();
      } else setError(se.message || "The answer was not sent.");
    } finally {
      setBusy(false);
    }
  };
  const tail = prompt?.tail ?? [];
  const question = [...tail].reverse().find((l) => l.trim() !== "");
  const since = ageWords(ask.since, now);
  return /* @__PURE__ */ jsxs("div", { className: "cs-need", children: [
    /* @__PURE__ */ jsxs("div", { children: [
      /* @__PURE__ */ jsx("b", { children: ask.label }),
      " asks",
      question ? ": " : " ",
      question ? /* @__PURE__ */ jsxs("span", { className: "cs-q", children: [
        "“",
        question.length > 140 ? `${question.slice(0, 140)}...` : question,
        "”"
      ] }) : null,
      " ",
      /* @__PURE__ */ jsx("em", { children: since === "age unknown" ? since : `about ${since}` })
    ] }),
    gone ? /* @__PURE__ */ jsx(ErrorNotice, { message: `${ask.label} is no longer waiting. Nothing was sent.` }) : /* @__PURE__ */ jsxs(Fragment, { children: [
      tail.length > 0 ? /* @__PURE__ */ jsxs(Fragment, { children: [
        /* @__PURE__ */ jsx(Button, { quiet: true, "aria-expanded": show, "aria-controls": `${id}-tail`, onClick: () => setShow((s) => !s), children: show ? "Hide prompt" : "Show prompt" }),
        show ? /* @__PURE__ */ jsx("pre", { id: `${id}-tail`, className: "cs-pre", tabIndex: 0, "aria-label": `Last lines from ${ask.label}`, children: tail.join("\n") }) : null
      ] }) : null,
      loadError ? /* @__PURE__ */ jsxs("p", { className: "cs-note", children: [
        "The prompt could not be loaded (",
        loadError,
        "). You can still type an answer."
      ] }) : null,
      prompt && prompt.suggested.length > 0 ? /* @__PURE__ */ jsx("div", { className: "cs-row", children: prompt.suggested.map((s, i) => (
        // every suggested answer is secondary: the page cannot tell a safe answer from a risky one
        /* @__PURE__ */ jsx(Button, { disabled: !!disabledReason, busy, title: disabledReason ?? void 0, onClick: () => send(s.send), children: s.label }, `${s.send}-${i}`)
      )) }) : prompt || loadError ? /* @__PURE__ */ jsxs(
        "form",
        {
          className: "cs-form",
          onSubmit: (e) => {
            e.preventDefault();
            send(text.trim());
          },
          children: [
            /* @__PURE__ */ jsxs("label", { className: "cs-sr", htmlFor: `${id}-a`, children: [
              "Answer for ",
              ask.label
            ] }),
            /* @__PURE__ */ jsx(Input, { id: `${id}-a`, className: "cs-input", value: text, onChange: (e) => setText(e.target.value), maxLength: 2e3, placeholder: "Type an answer...", disabled: !!disabledReason }),
            /* @__PURE__ */ jsx(Button, { type: "submit", primary: true, disabled: !!disabledReason || !text.trim(), busy, title: disabledReason ?? void 0, children: sent ? "Sent" : "Send answer" })
          ]
        }
      ) : null,
      sent && prompt && prompt.suggested.length > 0 ? /* @__PURE__ */ jsx("p", { className: "cs-note", role: "status", children: "Answer sent." }) : null,
      /* @__PURE__ */ jsx(ErrorNotice, { message: error })
    ] })
  ] });
}
function ErrorNeed({ ask, now }) {
  return /* @__PURE__ */ jsxs("div", { className: "cs-need cs-need-error", children: [
    /* @__PURE__ */ jsxs("div", { children: [
      /* @__PURE__ */ jsx(Dot, { status: "error" }),
      " ",
      /* @__PURE__ */ jsx("b", { children: ask.label }),
      " reported an error ",
      /* @__PURE__ */ jsx("em", { children: ageWords(ask.since, now) === "age unknown" ? "age unknown" : `about ${ageWords(ask.since, now)}` })
    ] }),
    /* @__PURE__ */ jsx("p", { className: "cs-note", children: "The shelf cannot answer an error. Look at that terminal in CAO." })
  ] });
}
const DELIVERY = {
  delivered: "Message delivered to the conductor of {name}.",
  pending: "Message queued: the conductor of {name} is busy and CAO will deliver it when it is idle.",
  failed: "CAO could not deliver the message to the conductor of {name}.",
  unknown: "CAO accepted the message for {name}; its delivery could not be confirmed."
};
function AskForm({ session, onClose }) {
  const { api, disabledReason, announce, refresh } = useShelfCtx();
  const id = useId();
  const input = useRef(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(false);
  const [outcome, setOutcome] = useState(null);
  const [error, setError] = useState(null);
  const [changed, setChanged] = useState(false);
  const timer = useRef(null);
  useEffect(() => {
    input.current?.focus();
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, []);
  const submit = async (e) => {
    e.preventDefault();
    const message = text.trim();
    if (!message || busy || disabledReason) return;
    setBusy(true);
    setError(null);
    setChanged(false);
    setOutcome(null);
    try {
      const r = await api.ask(session.name, { message, conductor_id: session.conductor.id });
      setText("");
      const said = DELIVERY[r.delivery] ?? DELIVERY.unknown;
      setSent(r.delivery === "delivered");
      setOutcome(said.replace("{name}", session.name));
      announce(said.replace("{name}", session.name));
      timer.current = setTimeout(() => setSent(false), 2e3);
      refresh();
    } catch (err) {
      const se = toShelfError(err);
      if (se.code === "conflict_changed") setChanged(true);
      else setError(se.message || "The message was not sent.");
    } finally {
      setBusy(false);
    }
  };
  return /* @__PURE__ */ jsxs(Fragment, { children: [
    /* @__PURE__ */ jsxs("form", { className: "cs-form", onSubmit: submit, onKeyDown: (e) => e.key === "Escape" && (e.stopPropagation(), onClose()), children: [
      /* @__PURE__ */ jsxs("label", { className: "cs-sr", htmlFor: `${id}-q`, children: [
        "Message to the conductor of ",
        session.name
      ] }),
      /* @__PURE__ */ jsx(Input, { id: `${id}-q`, ref: input, className: "cs-input", placeholder: "Ask the conductor...", value: text, onChange: (e) => setText(e.target.value), maxLength: 8e3, disabled: !!disabledReason }),
      /* @__PURE__ */ jsx(Button, { type: "submit", primary: true, disabled: !!disabledReason || !text.trim(), busy, title: disabledReason ?? void 0, children: sent ? "Sent" : "Send" })
    ] }),
    outcome ? /* @__PURE__ */ jsx("p", { className: "cs-note", role: "status", children: outcome }) : null,
    changed ? /* @__PURE__ */ jsx(ErrorNotice, { message: "This session changed, reload. Nothing was sent.", footer: /* @__PURE__ */ jsx(Button, { onClick: refresh, children: "Reload" }) }) : null,
    /* @__PURE__ */ jsx(ErrorNotice, { message: error })
  ] });
}
function Rows({ session, terminalId }) {
  const { api, now } = useShelfCtx();
  const [rows, setRows] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    let dead = false;
    api.historyRows(session, terminalId).then((r) => !dead && setRows(r.rows)).catch((e) => !dead && setError(toShelfError(e).message));
    return () => {
      dead = true;
    };
  }, [api, session, terminalId]);
  if (error) return /* @__PURE__ */ jsx(ErrorNotice, { message: error });
  if (!rows) return /* @__PURE__ */ jsx("div", { className: "cs-rows", children: "Loading rows..." });
  return /* @__PURE__ */ jsxs("div", { className: "cs-rows", children: [
    rows.length === 0 ? "No rows." : null,
    rows.map((r) => /* @__PURE__ */ jsxs("div", { children: [
      fmtAge(ageSince(r.created_at, now)),
      " ago [",
      r.status,
      "] ",
      r.text
    ] }, String(r.id)))
  ] });
}
function HistoryDrawer({ session }) {
  const { api, now } = useShelfCtx();
  const id = useId();
  const [h, setH] = useState(null);
  const [error, setError] = useState(null);
  const [open, setOpen] = useState({});
  useEffect(() => {
    let dead = false;
    api.history(session).then((r) => !dead && setH(r)).catch((e) => !dead && setError(toShelfError(e).message || "History could not be read."));
    return () => {
      dead = true;
    };
  }, [api, session]);
  return /* @__PURE__ */ jsxs("div", { className: "cs-hist", id, children: [
    /* @__PURE__ */ jsx("b", { children: "Closed helpers" }),
    " ",
    /* @__PURE__ */ jsx("small", { children: "rebuilt from inbox rows; their terminals no longer exist" }),
    /* @__PURE__ */ jsx(ErrorNotice, { className: "cs-gap", message: error }),
    !h && !error ? /* @__PURE__ */ jsx("small", { children: "Loading..." }) : null,
    h ? /* @__PURE__ */ jsxs(Fragment, { children: [
      h.closed.length === 0 ? /* @__PURE__ */ jsx("small", { children: "none recorded" }) : null,
      /* @__PURE__ */ jsx("ul", { children: h.closed.map((c) => /* @__PURE__ */ jsxs("li", { children: [
        /* @__PURE__ */ jsx("b", { children: helperName(c.label, c.terminal_id) }),
        ", ",
        c.rows,
        " ",
        c.rows === 1 ? "row" : "rows",
        ", last message ",
        fmtAge(ageSince(c.last_at, now)),
        " ago",
        " ",
        /* @__PURE__ */ jsx(Button, { quiet: true, "aria-expanded": !!open[c.terminal_id], onClick: () => setOpen((o) => ({ ...o, [c.terminal_id]: !o[c.terminal_id] })), children: "inbox rows" }),
        /* @__PURE__ */ jsxs("small", { children: [
          "last status: ",
          c.last_status
        ] }),
        open[c.terminal_id] ? /* @__PURE__ */ jsx(Rows, { session, terminalId: c.terminal_id }) : null
      ] }, c.terminal_id)) }),
      h.external_senders.length > 0 ? /* @__PURE__ */ jsxs("small", { children: [
        "Other senders (still sending, not helpers): ",
        h.external_senders.map((s) => `${s.sender_id} (${s.rows} ${s.rows === 1 ? "row" : "rows"})`).join(", ")
      ] }) : null,
      !h.complete ? /* @__PURE__ */ jsxs("small", { children: [
        "Only the newest ",
        h.window_rows,
        " inbox rows were read, so older closed helpers may be missing."
      ] }) : null
    ] }) : null
  ] });
}
const MAX_CHIPS = 5;
const POLL_NOTE = { hot: " (read every poll)", parked: " (read about every 15 s)", stale: " (read about every 60 s)" };
const TIER_WORD = { needs_you: "needs you", live: "live", parked: "parked", stale: "stale" };
function tagVariant(s) {
  if (s.tier === "needs_you") return s.tier_reason === "error" ? "err" : "warn";
  return s.tier === "live" ? "ok" : "muted";
}
function tagText(s, now) {
  const age = ageWords(s.last_active, now);
  switch (s.tier) {
    case "needs_you":
      return s.tier_reason === "error" ? "NEEDS YOU, error" : "NEEDS YOU";
    case "live":
      return "LIVE";
    case "parked":
      if (s.tier_reason === "age_unknown") return "PARKED, age unknown";
      return s.warm ? `WARM, idle ${age}` : `PARKED, idle ${age}`;
    default:
      return `STALE, idle ${age}`;
  }
}
function SessionCard({
  s,
  onStop,
  onRemote
}) {
  const { now, disabledReason, reasonId } = useShelfCtx();
  const id = useId();
  const [expanded, setExpanded] = useState(false);
  const [ask, setAsk] = useState(false);
  const [hist, setHist] = useState(false);
  const c = s.conductor;
  const chipped = s.conductor_missing ? [c, ...s.helpers] : s.helpers;
  const shown = chipped.slice(0, MAX_CHIPS);
  const more = chipped.length - shown.length;
  const all = [c, ...s.helpers];
  const orphans = all.filter((t) => t.orphan);
  const askOff = s.conductor_missing;
  const askReason = askOff ? "This session has no conductor, so there is nobody to ask." : disabledReason;
  const hasError = s.tier === "needs_you" && s.tier_reason === "error";
  const remoteReason = s.conductor_missing ? NO_CONDUCTOR : disabledReason;
  const offNote = `${id}-off`;
  const askDescribed = askOff ? offNote : reasonId;
  const remoteDescribed = s.conductor_missing ? offNote : reasonId;
  return /* @__PURE__ */ jsxs(Card, { role: "article", className: `cs-card cs-k-${s.tier}${hasError ? " cs-err" : ""}`, "aria-label": `session ${s.name}, ${TIER_WORD[s.tier]}`, children: [
    /* @__PURE__ */ jsxs("div", { className: "cs-head", children: [
      /* @__PURE__ */ jsx("button", { type: "button", className: "cs-ttl", "aria-expanded": expanded, "aria-controls": `${id}-det`, onClick: () => setExpanded((e) => !e), children: /* @__PURE__ */ jsx("span", { className: "cs-nm", children: s.name }) }),
      /* @__PURE__ */ jsx(Badge, { variant: tagVariant(s), className: "cs-tag", children: tagText(s, now) })
    ] }),
    s.conductor_missing ? /* @__PURE__ */ jsxs("div", { className: "cs-cond", children: [
      /* @__PURE__ */ jsx("span", { className: "cs-prof", children: "no conductor" }),
      /* @__PURE__ */ jsx("small", { className: "cs-muted", children: "it was deleted; every terminal here is a helper of it" })
    ] }) : /* @__PURE__ */ jsxs("div", { className: "cs-cond", children: [
      /* @__PURE__ */ jsx(Dot, { status: c.status }),
      /* @__PURE__ */ jsxs("span", { className: "cs-prof", children: [
        "conductor",
        c.profile ? ` (${c.profile})` : ""
      ] }),
      /* @__PURE__ */ jsx("small", { className: "cs-muted", children: statusText(c, now) })
    ] }),
    chipped.length > 0 ? /* @__PURE__ */ jsxs("ul", { className: "cs-chips", "aria-label": `helpers of ${s.name}`, children: [
      shown.map((h) => /* @__PURE__ */ jsx(Chip, { t: h }, h.id)),
      more > 0 ? /* @__PURE__ */ jsx("li", { children: /* @__PURE__ */ jsxs("button", { type: "button", className: "cs-chip", onClick: () => setExpanded(true), "aria-label": `${more} more helpers, show details`, children: [
        "+",
        more
      ] }) }) : null
    ] }) : null,
    orphans.map((o) => /* @__PURE__ */ jsxs("p", { className: "cs-note cs-note-orphan", children: [
      o.label,
      ": caller no longer exists"
    ] }, o.id)),
    s.conductor_missing ? /* @__PURE__ */ jsx("p", { className: "cs-note cs-note-orphan", id: offNote, children: "Ask is off: with no conductor there is nobody to ask. You can still answer a waiting helper or stop the session." }) : null,
    s.asks.map((a) => a.status === "waiting_user_answer" ? /* @__PURE__ */ jsx(AnswerBox, { ask: a }, a.terminal_id) : /* @__PURE__ */ jsx(ErrorNeed, { ask: a, now }, a.terminal_id)),
    s.in_flight ? /* @__PURE__ */ jsxs("div", { className: "cs-hof", children: [
      "conductor working, waiting on helper ",
      s.in_flight.label
    ] }) : null,
    expanded ? /* @__PURE__ */ jsxs("div", { className: "cs-det", id: `${id}-det`, children: [
      /* @__PURE__ */ jsxs("table", { children: [
        /* @__PURE__ */ jsxs("caption", { className: "cs-sr", children: [
          "Terminals of ",
          s.name
        ] }),
        /* @__PURE__ */ jsx("thead", { children: /* @__PURE__ */ jsxs("tr", { children: [
          /* @__PURE__ */ jsx("th", { scope: "col", children: "terminal" }),
          /* @__PURE__ */ jsx("th", { scope: "col", children: "status" }),
          /* @__PURE__ */ jsx("th", { scope: "col", children: "last_active" }),
          /* @__PURE__ */ jsx("th", { scope: "col", children: "caller_id" })
        ] }) }),
        /* @__PURE__ */ jsx("tbody", { children: all.map((t) => /* @__PURE__ */ jsxs("tr", { children: [
          /* @__PURE__ */ jsx("td", { children: t.id }),
          /* @__PURE__ */ jsx("td", { children: t.status }),
          /* @__PURE__ */ jsx("td", { title: t.last_active, children: t.last_active ? `${ageWords(t.last_active, now)} ago` : "age unknown" }),
          /* @__PURE__ */ jsx("td", { children: t.role === "conductor" ? "none" : t.role === "peer" ? "none (peer)" : t.orphan ? `${t.caller_id} (gone)` : t.caller_id })
        ] }, t.id)) })
      ] }),
      /* @__PURE__ */ jsxs("p", { children: [
        "Status is read off the screen, so treat it as a hint. last_active is the better clock. Poll class: ",
        s.poll_class,
        POLL_NOTE[s.poll_class],
        "."
      ] })
    ] }) : null,
    /* @__PURE__ */ jsx(RelayControls, { s }),
    /* @__PURE__ */ jsxs("div", { className: "cs-acts", children: [
      /* @__PURE__ */ jsx(Button, { "aria-expanded": ask, onClick: () => setAsk((a) => !a), disabled: !!askReason, title: askReason ?? void 0, "aria-describedby": askReason ? askDescribed : void 0, children: "Ask" }),
      /* @__PURE__ */ jsxs(Button, { "aria-expanded": hist, onClick: () => setHist((h) => !h), children: [
        "History",
        s.history_count === null ? "" : ` (${s.history_count})`
      ] }),
      /* @__PURE__ */ jsx(Button, { "aria-haspopup": "dialog", onClick: () => onRemote(s), disabled: !!remoteReason, title: remoteReason ?? void 0, "aria-describedby": remoteReason ? remoteDescribed : void 0, children: "Remote control" }),
      /* @__PURE__ */ jsx(Button, { danger: true, onClick: (e) => onStop(s.name, e.currentTarget), disabled: !!disabledReason, title: disabledReason ?? void 0, "aria-describedby": reasonId, children: "Stop..." })
    ] }),
    ask && !askOff ? /* @__PURE__ */ jsx(AskForm, { session: s, onClose: () => setAsk(false) }) : null,
    hist ? /* @__PURE__ */ jsx(HistoryDrawer, { session: s.name }) : null
  ] });
}
const REMEMBER = "Remember closed helpers (ids and times only, no text)";
function Settings({ onClose }) {
  const { api, announce, refresh } = useShelfCtx();
  const id = useId();
  const [cfg, setCfg] = useState(null);
  const [error, setError] = useState(null);
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    let dead = false;
    api.getConfig().then((c) => !dead && setCfg(c)).catch((e) => !dead && setError(toShelfError(e).message));
    return () => {
      dead = true;
    };
  }, [api]);
  const set = (k, v) => cfg && setCfg({ ...cfg, [k]: v });
  const num = (s) => s === "" || Number.isNaN(Number(s)) ? 0 : Number(s);
  const save = async (e) => {
    e.preventDefault();
    if (!cfg) return;
    setError(null);
    setSaved(false);
    try {
      const c = await api.putConfig(cfg);
      setCfg(c);
      setSaved(true);
      announce("Settings saved");
      refresh();
    } catch (err) {
      setError(toShelfError(err).message || "Settings were not saved.");
    }
  };
  return /* @__PURE__ */ jsx(Card, { className: "cs-box", children: /* @__PURE__ */ jsxs("form", { onSubmit: save, "aria-label": "Settings", onKeyDown: (e) => e.key === "Escape" && onClose(), children: [
    /* @__PURE__ */ jsx(CardTitle, { className: "cs-box-title", children: "Settings" }),
    !cfg && !error ? /* @__PURE__ */ jsx("p", { className: "cs-muted", children: "Loading..." }) : null,
    cfg ? /* @__PURE__ */ jsxs(Fragment, { children: [
      /* @__PURE__ */ jsx("label", { htmlFor: `${id}-u`, children: "CAO address" }),
      /* @__PURE__ */ jsx(Input, { id: `${id}-u`, className: "cs-input", value: cfg.cao_url, onChange: (e) => set("cao_url", e.target.value), "aria-describedby": `${id}-uh` }),
      /* @__PURE__ */ jsx("small", { id: `${id}-uh`, className: "cs-help", children: "Must be http and loopback only: 127.0.0.1, localhost or ::1. Anything else is refused." }),
      /* @__PURE__ */ jsx("label", { htmlFor: `${id}-s`, children: "Refresh every (seconds, 3 to 60)" }),
      /* @__PURE__ */ jsx(Input, { id: `${id}-s`, type: "number", className: "cs-input", min: 3, max: 60, value: cfg.poll_seconds, onChange: (e) => set("poll_seconds", num(e.target.value)) }),
      /* @__PURE__ */ jsx("label", { htmlFor: `${id}-w`, children: "Warm below (hours, 0.25 to 24)" }),
      /* @__PURE__ */ jsx(Input, { id: `${id}-w`, type: "number", step: "any", className: "cs-input", min: 0.25, max: 24, value: cfg.warm_hours, onChange: (e) => set("warm_hours", num(e.target.value)) }),
      /* @__PURE__ */ jsx("label", { htmlFor: `${id}-t`, children: "Stale from (hours, 2 to 720)" }),
      /* @__PURE__ */ jsx(Input, { id: `${id}-t`, type: "number", step: "any", className: "cs-input", min: 2, max: 720, value: cfg.stale_hours, onChange: (e) => set("stale_hours", num(e.target.value)) }),
      /* @__PURE__ */ jsxs("div", { className: "cs-switch", children: [
        /* @__PURE__ */ jsx(Toggle, { checked: cfg.persist_history, onChange: (v) => set("persist_history", v), label: REMEMBER }),
        /* @__PURE__ */ jsx("span", { "aria-hidden": "true", onClick: () => set("persist_history", !cfg.persist_history), children: REMEMBER })
      ] })
    ] }) : null,
    /* @__PURE__ */ jsx(ErrorNotice, { className: "cs-gap", message: error }),
    saved ? /* @__PURE__ */ jsx("p", { role: "status", children: "Saved." }) : null,
    /* @__PURE__ */ jsxs("div", { className: "cs-foot", children: [
      /* @__PURE__ */ jsx(Button, { type: "submit", primary: true, disabled: !cfg, children: "Save" }),
      /* @__PURE__ */ jsx(Button, { onClick: onClose, children: "Close" })
    ] })
  ] }) });
}
const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,58}$/;
function newStartKey() {
  const c = globalThis.crypto;
  if (c?.randomUUID) return c.randomUUID();
  return `k${Math.random().toString(36).slice(2)}${Math.random().toString(36).slice(2)}${Date.now().toString(36)}`.padEnd(20, "0");
}
function StartDialog({ onClose }) {
  const { api, announce, refresh } = useShelfCtx();
  const id = useId();
  const [profiles, setProfiles] = useState(null);
  const [error, setError] = useState(null);
  const [profile, setProfile] = useState("");
  const [provider, setProvider] = useState("");
  const [cwd, setCwd] = useState("");
  const [message, setMessage] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [remote, setRemote] = useState(false);
  const [accepted, setAccepted] = useState(false);
  const [key] = useState(newStartKey);
  const openChat = useOpenChat();
  useEffect(() => {
    let dead = false;
    api.profiles().then((r) => {
      if (dead) return;
      setProfiles(r.profiles);
      if (r.profiles[0]) setProfile((p) => p || r.profiles[0].name);
    }).catch((e) => !dead && setError(toShelfError(e).message || "Profiles could not be read."));
    return () => {
      dead = true;
    };
  }, [api]);
  const nameBad = name !== "" && !NAME_RE.test(name);
  const complete = profile !== "" && cwd.trim() !== "" && !nameBad && (!remote || accepted);
  const ready = complete && !busy;
  const dirty = provider !== "" || cwd !== "" || message !== "" || name !== "" || remote;
  const chosen = profiles?.find((p) => p.name === profile);
  const submit = async (e) => {
    e.preventDefault();
    if (!ready) return;
    setBusy(true);
    setError(null);
    const fields = {
      idempotency_key: key,
      agent_profile: profile,
      working_directory: cwd.trim(),
      ...provider.trim() ? { provider: provider.trim() } : {},
      ...name ? { name } : {}
    };
    if (remote) {
      try {
        const r = await api.relayCreate({ start: fields, acknowledge_risk: true });
        if (openChat) {
          openChat(r.slot_key);
          announce(`Session ${r.session} started; its remote control chat is open`);
        } else {
          announce(`Session ${r.session} started. Open the chat "${r.title}" from the Kiro Crew sidebar.`);
        }
        refresh();
        onClose();
      } catch (err) {
        setError(relayErrorText(toShelfError(err)));
        setBusy(false);
      }
      return;
    }
    try {
      const r = await api.start({ ...fields, ...message.trim() ? { message: message.trim() } : {} });
      announce(`Session ${r.session} started`);
      refresh();
      onClose();
    } catch (err) {
      setError(toShelfError(err).message || "The session was not started.");
      setBusy(false);
    }
  };
  const formId = `${id}-f`;
  const footer = /* @__PURE__ */ jsxs("div", { className: "cs-actions", children: [
    /* @__PURE__ */ jsx(Button, { onClick: onClose, children: "Cancel" }),
    /* @__PURE__ */ jsx(Button, { type: "submit", form: formId, primary: true, disabled: !complete, busy, children: busy ? "Starting..." : "Start" })
  ] });
  return /* @__PURE__ */ jsxs(Modal, { open: true, onClose, title: "Start a session", maxWidth: 520, guardAccidentalDismiss: dirty, dismissDisabled: busy, footer, children: [
    /* @__PURE__ */ jsx("small", { className: "cs-small", children: "Nothing is sent until you press Start." }),
    /* @__PURE__ */ jsxs("form", { id: formId, onSubmit: submit, children: [
      /* @__PURE__ */ jsx("span", { className: "cs-field", children: "Profile" }),
      /* @__PURE__ */ jsx(
        Select,
        {
          "aria-label": "Profile",
          className: "cs-select",
          options: profiles?.map((p) => p.name) ?? [],
          value: profile,
          onChange: (v) => v && setProfile(v),
          clearLabel: profiles === null ? "Loading profiles..." : profiles.length === 0 ? "No profiles found" : void 0,
          disabled: !profiles || profiles.length === 0
        }
      ),
      chosen?.description ? /* @__PURE__ */ jsx("small", { className: "cs-help", children: chosen.description }) : null,
      /* @__PURE__ */ jsx("label", { className: "cs-field", htmlFor: `${id}-v`, children: "Provider (optional)" }),
      /* @__PURE__ */ jsx(Input, { id: `${id}-v`, className: "cs-input", value: provider, placeholder: chosen?.provider ?? "profile default", onChange: (e) => setProvider(e.target.value) }),
      /* @__PURE__ */ jsx("label", { className: "cs-field", htmlFor: `${id}-d`, children: "Working directory (required)" }),
      /* @__PURE__ */ jsx(Input, { id: `${id}-d`, className: "cs-input", value: cwd, onChange: (e) => setCwd(e.target.value), required: true }),
      remote ? /* @__PURE__ */ jsx("small", { className: "cs-help", children: "Your first message goes through the relay chat. The session's first message is the remote-control handshake." }) : /* @__PURE__ */ jsxs(Fragment, { children: [
        /* @__PURE__ */ jsx("label", { className: "cs-field", htmlFor: `${id}-m`, children: "First message (optional)" }),
        /* @__PURE__ */ jsx("textarea", { id: `${id}-m`, className: "cs-textarea", rows: 3, maxLength: 8e3, value: message, onChange: (e) => setMessage(e.target.value) })
      ] }),
      /* @__PURE__ */ jsx("label", { className: "cs-field", htmlFor: `${id}-n`, children: "Session name (optional)" }),
      /* @__PURE__ */ jsx(Input, { id: `${id}-n`, className: "cs-input", value: name, onChange: (e) => setName(e.target.value), "aria-invalid": nameBad, "aria-describedby": nameBad ? `${id}-ne` : void 0 }),
      nameBad ? /* @__PURE__ */ jsx("small", { id: `${id}-ne`, className: "cs-bad", role: "alert", children: "Letters, digits, dash and underscore only, starting with a letter or digit, up to 59 characters." }) : null,
      /* @__PURE__ */ jsxs("label", { className: "cs-checkrow", children: [
        /* @__PURE__ */ jsx("input", { type: "checkbox", className: "cs-check", checked: remote, onChange: (e) => setRemote(e.target.checked) }),
        "and remote control it"
      ] }),
      remote ? /* @__PURE__ */ jsxs(Fragment, { children: [
        /* @__PURE__ */ jsx(RiskText, { shortId: `${id}-risk` }),
        /* @__PURE__ */ jsxs("label", { className: "cs-checkrow", children: [
          /* @__PURE__ */ jsx("input", { type: "checkbox", className: "cs-check", checked: accepted, onChange: (e) => setAccepted(e.target.checked), "aria-describedby": `${id}-risk` }),
          "I accept this risk"
        ] })
      ] }) : null,
      /* @__PURE__ */ jsx(ErrorNotice, { className: "cs-gap", message: error })
    ] })
  ] });
}
const OUTCOME = {
  stopped: "stopped",
  skipped_changed: "skipped, it changed since the list was made",
  skipped_working: "skipped, it is working or waiting",
  skipped_not_in_plan: "skipped, it was not in the list",
  not_found: "already gone",
  failed: "failed"
};
function StopDialog({ target, onClose }) {
  const { api, now, announce, refresh } = useShelfCtx();
  const stale = target.scope === "stale";
  const [plan, setPlan] = useState(null);
  const [error, setError] = useState(null);
  const [ticked, setTicked] = useState({});
  const [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false);
  const [results, setResults] = useState(null);
  const closeBtn = useRef(null);
  useEffect(() => {
    if (results) closeBtn.current?.focus();
  }, [results]);
  useEffect(() => {
    let dead = false;
    api.stopPlan(target.scope === "stale" ? { scope: "stale" } : { scope: "named", sessions: [target.name] }).then((p) => !dead && setPlan(p)).catch((e) => !dead && setError(toShelfError(e).message || "The list could not be made."));
    return () => {
      dead = true;
    };
  }, [api, target]);
  const sessions = plan?.sessions ?? [];
  const picked = sessions.filter((s) => ticked[s.name]);
  const terminals = picked.reduce((n, s) => n + s.terminals.length, 0);
  const workingListed = sessions.filter((s) => s.working);
  const workingPicked = picked.filter((s) => s.working);
  const needAck = !stale && workingPicked.length > 0;
  const stoppable = !!plan && picked.length > 0 && (!needAck || ack) && !results;
  const canStop = stoppable && !busy;
  const execute = async () => {
    if (!plan || !canStop) return;
    setBusy(true);
    setError(null);
    try {
      const r = await api.stopExecute({
        plan_id: plan.plan_id,
        sessions: picked.map((s) => needAck && s.working ? { name: s.name, last_active: s.last_active, acknowledge_working: true } : { name: s.name, last_active: s.last_active })
      });
      setResults(r.results);
      const n = r.results.filter((x) => x.outcome === "stopped").length;
      announce(`Stopped ${n} of ${r.results.length} sessions`);
      refresh();
    } catch (e) {
      const se = toShelfError(e);
      setError(se.code === "plan_expired" ? "This list expired. Close the dialog and open it again." : se.message || "Stopping failed.");
    } finally {
      setBusy(false);
    }
  };
  const title = stale ? "Clean up stale sessions" : `Stop session ${target.name}?`;
  const footer = results ? /* @__PURE__ */ jsx("div", { className: "cs-actions", children: /* @__PURE__ */ jsx(Button, { primary: true, ref: closeBtn, onClick: onClose, children: "Done" }) }) : /* @__PURE__ */ jsxs("div", { className: "cs-actions", children: [
    stale && sessions.length > 0 ? /* @__PURE__ */ jsx(Button, { quiet: true, className: "cs-push", onClick: () => setTicked(Object.fromEntries(sessions.map((s) => [s.name, true]))), children: "Select all" }) : null,
    /* @__PURE__ */ jsx(Button, { onClick: onClose, disabled: busy, title: busy ? "Stopping is in progress; the result shows here." : void 0, children: "Cancel" }),
    /* @__PURE__ */ jsxs(Button, { danger: true, disabled: !stoppable, busy, onClick: execute, children: [
      "Stop ",
      picked.length,
      " session",
      picked.length === 1 ? "" : "s",
      " (",
      terminals,
      " terminal",
      terminals === 1 ? "" : "s",
      ")"
    ] })
  ] });
  return /* @__PURE__ */ jsx(Modal, { open: true, onClose, title: /* @__PURE__ */ jsx("span", { className: "cs-dlg-title", children: title }), maxWidth: 520, dismissDisabled: busy, footer, children: results ? /* @__PURE__ */ jsx("ul", { className: "cs-list", "aria-label": "Results", children: results.map((r) => /* @__PURE__ */ jsxs("li", { children: [
    /* @__PURE__ */ jsx("b", { children: r.name }),
    ": ",
    OUTCOME[r.outcome],
    r.message ? /* @__PURE__ */ jsxs("small", { children: [
      " (",
      r.message,
      ")"
    ] }) : null
  ] }, r.name)) }) : /* @__PURE__ */ jsxs("div", { className: "cs-stack", children: [
    /* @__PURE__ */ jsx("small", { className: "cs-small", children: "Nothing is stopped until you press the red button. A stopped session does not come back." }),
    /* @__PURE__ */ jsx(ErrorNotice, { message: error }),
    !plan && !error ? /* @__PURE__ */ jsx("p", { role: "status", children: "Making the list..." }) : null,
    plan && sessions.length === 0 ? /* @__PURE__ */ jsx("p", { children: "Nothing is stale right now." }) : null,
    plan?.truncated ? /* @__PURE__ */ jsxs("p", { className: "cs-note", children: [
      "Only the first ",
      sessions.length,
      " stale sessions (oldest idle first) are listed. Run clean-up again for the rest."
    ] }) : null,
    sessions.length > 0 ? /* @__PURE__ */ jsx("ul", { className: "cs-list", children: sessions.map((s) => /* @__PURE__ */ jsx("li", { children: /* @__PURE__ */ jsxs("label", { className: "cs-checkrow", children: [
      /* @__PURE__ */ jsx("input", { type: "checkbox", className: "cs-check", checked: !!ticked[s.name], onChange: (e) => setTicked((t) => ({ ...t, [s.name]: e.target.checked })) }),
      /* @__PURE__ */ jsxs("span", { children: [
        /* @__PURE__ */ jsx("b", { children: s.name }),
        ", ",
        s.last_active ? "idle " : "",
        ageWords(s.last_active, now),
        s.working ? " (working or waiting)" : "",
        /* @__PURE__ */ jsxs("small", { className: "cs-small", children: [
          "stops ",
          s.terminals.length,
          " terminal",
          s.terminals.length === 1 ? "" : "s",
          ": ",
          s.terminals.map((t) => t.role === "conductor" ? "conductor" : t.label).join(", ")
        ] })
      ] })
    ] }) }, s.name)) }) : null,
    workingListed.length > 0 ? /* @__PURE__ */ jsx(
      ErrorNotice,
      {
        title: "Heads up:",
        message: `${workingListed.map((s) => s.name).join(", ")} ${workingListed.length === 1 ? "is" : "are"} working or waiting for you right now. Stopping it throws that work away.`
      }
    ) : null,
    needAck ? /* @__PURE__ */ jsxs("label", { className: "cs-checkrow", children: [
      /* @__PURE__ */ jsx("input", { type: "checkbox", className: "cs-check", checked: ack, onChange: (e) => setAck(e.target.checked) }),
      /* @__PURE__ */ jsx("span", { children: "I understand this throws away work in progress." })
    ] }) : null
  ] }) });
}
function TierToggles({ data, visible, onToggle }) {
  const t = data.counts.by_tier;
  const items = [
    ["needs_you", "Needs you", t.needs_you],
    ["live", "Live", t.live],
    ["parked", "Parked", t.parked],
    ["stale", `Stale, over ${hours(data.thresholds.stale_hours)} h`, t.stale]
  ];
  return /* @__PURE__ */ jsx("div", { className: "cs-tiers", role: "group", "aria-label": "Tiers to show", children: items.map(([k, label, n]) => /* @__PURE__ */ jsxs(Button, { className: "cs-tier", "aria-pressed": visible[k], onClick: () => onToggle(k), children: [
    label,
    " ",
    /* @__PURE__ */ jsx("b", { children: n }),
    k === "stale" && !visible.stale ? " (hidden)" : ""
  ] }, k)) });
}
function HonestLine({ data }) {
  const s = data.counts.by_status;
  return /* @__PURE__ */ jsxs("p", { className: "cs-honest", children: [
    /* @__PURE__ */ jsx("b", { children: data.counts.terminals }),
    " terminals: ",
    /* @__PURE__ */ jsx("b", { children: s.processing }),
    " processing, ",
    /* @__PURE__ */ jsx("b", { children: s.waiting_user_answer }),
    " waiting for you,",
    " ",
    s.error > 0 ? /* @__PURE__ */ jsxs(Fragment, { children: [
      /* @__PURE__ */ jsx("b", { children: s.error }),
      " in error,",
      " "
    ] }) : null,
    s.completed > 0 ? /* @__PURE__ */ jsxs(Fragment, { children: [
      /* @__PURE__ */ jsx("b", { children: s.completed }),
      " completed,",
      " "
    ] }) : null,
    s.unknown > 0 ? /* @__PURE__ */ jsxs(Fragment, { children: [
      /* @__PURE__ */ jsx("b", { children: s.unknown }),
      " unknown,",
      " "
    ] }) : null,
    /* @__PURE__ */ jsx("b", { children: s.idle }),
    " idle. Idle is not running."
  ] });
}
const POLL_MS = 5e3;
const MIN_POLL_MS = 3e3;
const MAX_POLL_MS = 6e4;
const MAX_BACKOFF_MS = 6e4;
function pollMs(intervalSeconds) {
  if (typeof intervalSeconds !== "number" || !Number.isFinite(intervalSeconds)) return POLL_MS;
  return Math.min(MAX_POLL_MS, Math.max(MIN_POLL_MS, intervalSeconds * 1e3));
}
const isUnchanged = (r) => r.unchanged === true;
function useShelf() {
  const api = useShelfApi();
  const { active } = useAppInfo();
  const enabled = active !== false;
  const [data, setData] = useState(null);
  const [receivedAt, setReceivedAt] = useState(0);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const revision = useRef(null);
  const failures = useRef(0);
  const interval = useRef(POLL_MS);
  const timer = useRef(null);
  const ctrl = useRef(null);
  const loop = useRef(null);
  useEffect(() => {
    if (!enabled) return;
    let dead = false;
    const visible = () => document.visibilityState !== "hidden";
    const schedule = () => {
      if (dead || !visible()) return;
      const every = interval.current;
      const wait = failures.current === 0 ? every : Math.min(MAX_BACKOFF_MS, every * 2 ** failures.current);
      timer.current = setTimeout(run, wait);
    };
    const run = async () => {
      if (dead || !visible()) return;
      if (timer.current) clearTimeout(timer.current);
      ctrl.current?.abort();
      const c = new AbortController();
      ctrl.current = c;
      try {
        const r = await api.shelf(revision.current, c.signal);
        if (dead || c.signal.aborted) return;
        failures.current = 0;
        setError(null);
        if (!isUnchanged(r)) {
          revision.current = r.revision;
          interval.current = pollMs(r.interval_seconds);
          setData(r);
          setReceivedAt(Date.now());
        }
      } catch (e) {
        if (dead || c.signal.aborted) return;
        failures.current += 1;
        setError(toShelfError(e));
      } finally {
        if (!dead && !c.signal.aborted) setLoading(false);
      }
      schedule();
    };
    loop.current = run;
    const onVisibility = () => {
      if (visible()) run();
      else {
        if (timer.current) clearTimeout(timer.current);
        ctrl.current?.abort();
      }
    };
    document.addEventListener("visibilitychange", onVisibility);
    if (visible()) run();
    return () => {
      dead = true;
      loop.current = null;
      document.removeEventListener("visibilitychange", onVisibility);
      if (timer.current) clearTimeout(timer.current);
      ctrl.current?.abort();
    };
  }, [api, enabled]);
  const refresh = useCallback(() => {
    revision.current = null;
    loop.current?.();
  }, []);
  return { data, receivedAt, error, loading, refresh };
}
const CSS = `
.cs-root{font-size:14px;line-height:1.4;position:relative}
.cs-root *,.cs-root *::before,.cs-root *::after{box-sizing:border-box}
.cs-sr{position:absolute;width:1px;height:1px;margin:-1px;padding:0;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
.cs-muted{color:var(--muted)}
.cs-mono{font-family:var(--mono)}

/* buttons: host Btn through components/Button.tsx */
.cs-btn{justify-content:center;text-align:center}
.cs-btn[aria-expanded="true"]{background:var(--bg-hover);border-color:var(--border-strong)}
.cs-btn[aria-pressed="true"]{background:var(--accent-subtle);border-color:var(--accent);color:var(--text-strong)}
.cs-quiet{border-color:transparent}

/* tier toggles and the honest line */
.cs-tiers{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:4px;margin:0 -4px}
.cs-tier{white-space:nowrap;font-weight:600}
.cs-honest{margin:0;font-size:12px;color:var(--muted);padding:4px 0 8px}
.cs-honest b{color:var(--text)}

/* sections */
.cs-sec{margin:16px 0 8px;font-size:11.5px;font-weight:600;color:var(--muted);display:flex;align-items:center;gap:8px}
.cs-sec::after{content:"";flex:1;height:1px;background:var(--border)}
.cs-none{margin:0 0 8px;font-size:12px;color:var(--muted)}
.cs-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,300px),1fr));gap:12px;padding:0 0 8px}
.cs-dim .cs-card{opacity:.55;filter:saturate(.6)}

/* card: a host Card; no tier fill, so chips and muted text always sit on --card */
.cs-card{margin:0;padding:12px;display:flex;flex-direction:column;gap:8px;min-width:0;container-type:inline-size}
.cs-k-needs_you{box-shadow:inset 3px 0 0 var(--warn),var(--shadow-sm)}
.cs-k-needs_you.cs-err{box-shadow:inset 3px 0 0 var(--danger),var(--shadow-sm)}
.cs-k-stale{opacity:.66;filter:saturate(.5)}
.cs-card:hover,.cs-card:focus-within{opacity:1;filter:none}
.cs-head{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:4px 8px}
.cs-ttl{flex:1 1 auto;min-width:0;overflow-wrap:anywhere;background:transparent;border:0;margin:0;padding:2px 0;text-align:left;cursor:pointer;font:inherit;color:inherit;border-radius:var(--radius-md)}
.cs-nm{font-size:14px;font-weight:600;color:var(--text-strong)}
.cs-tag{flex:none}
.cs-cond{font-size:13px;display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.cs-cond .cs-prof{overflow-wrap:anywhere}
.cs-dot{display:inline-block;width:9px;height:9px;border-radius:50%;border:1.8px solid var(--muted);flex:none}
.cs-dot-processing{background:var(--ok);border-color:var(--ok);animation:cs-pulse 1.2s ease-in-out infinite}
.cs-dot-waiting_user_answer{background:var(--warn);border-color:var(--warn)}
.cs-dot-completed{background:var(--info);border-color:var(--info)}
.cs-dot-error{background:var(--danger);border-color:var(--danger)}
@keyframes cs-pulse{50%{box-shadow:0 0 0 4px color-mix(in srgb,var(--ok) 25%,transparent)}}
@media (prefers-reduced-motion:reduce){.cs-dot-processing{animation:none}.cs-skel{animation:none}}

/* helper chips: outline pills that wrap inside the card */
.cs-chips{list-style:none;margin:0;padding:0;display:flex;flex-wrap:wrap;gap:6px}
.cs-chip{display:inline-flex;flex-wrap:wrap;align-items:center;gap:2px 6px;max-width:100%;border:1px solid var(--border-strong);background:var(--card);border-radius:var(--radius-lg);padding:2px 10px 2px 8px;font:inherit;font-size:12px;color:inherit}
.cs-chip>span{min-width:0;overflow-wrap:anywhere}
.cs-chip small{color:var(--muted);font-size:12px}
.cs-chip-orphan{border:1.5px dashed var(--danger);background:var(--danger-subtle)}
.cs-chip-orphan small{color:var(--text)}
button.cs-chip{cursor:pointer}
.cs-note{margin:0;font-size:12px;color:var(--muted)}
/* status sentences are --text with a dot in the host status hue: a host -text token is only 3:1 on
   the card in some dark theme packs (design ruling 7), and a dot needs 3:1, words 4.5:1 */
.cs-note-orphan{color:var(--text)}
.cs-hof{margin:0;font-size:12px;color:var(--text);font-weight:600}
.cs-hof::before,.cs-note-orphan::before{content:"";display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:baseline}
.cs-hof::before{background:var(--ok)}
.cs-note-orphan::before{background:var(--danger)}

/* the question block */
.cs-need{padding:8px 10px;border-radius:var(--radius-md);background:var(--warn-subtle);border:1px solid var(--warn);font-size:13px;color:var(--text);display:flex;flex-direction:column;align-items:flex-start;gap:8px}
.cs-need-error{background:var(--danger-subtle);border-color:var(--danger)}
.cs-need em{font-style:normal;color:var(--text)}
.cs-need .cs-note{color:var(--text)}
.cs-need>*{max-width:100%}
.cs-need .cs-row{display:flex;gap:8px;flex-wrap:wrap}
/* on the tint a resting suggested answer keeps the card surface, so it reads as a button in dark mode too */
.cs-need .cs-row .cs-btn:not(:hover){background:var(--card)}
.cs-need .cs-form{align-self:stretch}
.cs-q{overflow-wrap:anywhere}
.cs-pre{font-family:var(--mono);font-size:12px;line-height:1.4;white-space:pre-wrap;overflow-wrap:anywhere;margin:0;padding:6px 8px;border-radius:var(--radius-md);background:var(--bg-hover);color:var(--text);border:1px solid var(--border);max-height:12em;overflow:auto;align-self:stretch}

/* relay: neutral on purpose, the only accent fills on a card are primary buttons */
.cs-relay{display:flex;flex-direction:column;gap:8px}
.cs-relay-on{padding:8px 10px;border-radius:var(--radius-md);border:1px solid var(--border-strong);background:var(--bg-hover);color:var(--text);font-size:13px;display:flex;flex-wrap:wrap;gap:2px 8px;align-items:baseline}
.cs-relay-on small{color:var(--text)}
.cs-relay-ttl{overflow-wrap:anywhere;min-width:0}
.cs-relay-on .cs-row{flex:1 1 100%;display:grid;grid-template-columns:repeat(auto-fit,minmax(8rem,1fr));gap:8px;margin-top:4px}
/* on the neutral fill a resting button keeps the card surface, so its edge shows in dark mode too; the host hover fill still applies */
.cs-relay-on .cs-btn:not(:hover){background:var(--card)}
.cs-notice{padding:8px 10px;border-radius:var(--radius-md);font-size:13px;color:var(--text)}
.cs-notice[data-tone="warn"]{background:var(--warn-subtle);border-left:3px solid var(--warn)}
.cs-notice .cs-disc summary{color:var(--warn-text);font-weight:600}

/* disclosure: native details, a chevron that turns when open */
.cs-disc{border:1px solid var(--border);border-radius:var(--radius-md);font-size:13px}
.cs-disc>summary{cursor:pointer;font-weight:600;padding:6px 10px;list-style:none;display:flex;align-items:center;gap:8px;border-radius:var(--radius-md)}
.cs-disc>summary::-webkit-details-marker{display:none}
.cs-disc>summary::before{content:"";width:6px;height:6px;border-right:2px solid currentColor;border-bottom:2px solid currentColor;transform:rotate(-45deg);flex:none}
.cs-disc[open]>summary::before{transform:rotate(45deg)}
.cs-disc[open]>summary{border-bottom:1px solid var(--border);border-radius:var(--radius-md) var(--radius-md) 0 0}
.cs-disc p{margin:8px 10px;overflow-wrap:anywhere}
.cs-disc--bare{border:0;margin-top:4px}
.cs-disc--bare>summary{padding:0;display:inline-flex}
.cs-disc--bare[open]>summary{border-bottom:0}
.cs-disc--bare p{margin:4px 0 0}
/* what remote control means: a sentence and a closed disclosure, once near the top of the page */
.cs-rc-intro{margin:4px 0 8px;font-size:13px;color:var(--text)}
.cs-rc-intro>p{margin:0;overflow-wrap:anywhere}
.cs-rc-intro .cs-disc p{max-width:72ch}

/* the action row: 2 by 2, 4 across on a wide card */
.cs-acts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:auto}
@container (min-width:460px){.cs-acts{grid-template-columns:repeat(4,auto)}}

/* details, history, forms */
.cs-det{font-size:12px;border-top:1px solid var(--border);padding-top:6px;overflow-x:auto}
.cs-det table{min-width:100%;width:max-content;border-collapse:collapse}
.cs-det th{text-align:left;color:var(--muted);font-weight:600;padding:1px 8px 1px 0;white-space:nowrap}
.cs-det td{padding:1px 8px 1px 0;font-family:var(--mono);font-size:12px;white-space:nowrap}
.cs-det p{margin:6px 0 0;color:var(--muted)}
.cs-hist{padding:8px 10px;background:var(--bg-hover);color:var(--text);border-radius:var(--radius-md);font-size:13px}
.cs-hist ul{list-style:none;margin:4px 0 0;padding:0}
.cs-hist li{margin:8px 0;display:flex;flex-wrap:wrap;align-items:center;gap:4px 8px}
.cs-hist small{color:var(--text);font-size:12px}
.cs-hist .cs-rows{flex:1 1 100%}
.cs-rows{margin:0;padding-left:8px;border-left:2px solid var(--border);font-family:var(--mono);font-size:12px;overflow-wrap:anywhere}
.cs-form{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.cs-input{width:100%;display:block}
.cs-form .cs-input{width:auto;flex:1 1 12rem}
.cs-textarea{width:100%;display:block;background:var(--bg-elevated);border:1px solid var(--border);border-radius:var(--radius-md);padding:8px 12px;color:var(--text);font:inherit;font-size:14px;resize:vertical}
.cs-foot{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:12px 0 4px}
.cs-foot>span{font-size:12px;color:var(--muted)}
.cs-box{margin:8px 0;padding:12px 16px;font-size:13px}
.cs-box-title{flex-wrap:wrap;margin:0 0 8px}
.cs-box-title small{font-weight:400;font-size:12px}
.cs-box ul{margin:4px 0 0;padding-left:18px}
.cs-box label{display:block;margin:12px 0 4px;font-weight:600}
.cs-box .cs-input{max-width:420px}
.cs-help{display:block;margin-top:4px;font-size:12px;color:var(--muted)}
.cs-switch{display:flex;gap:8px;align-items:center;margin-top:16px}
.cs-gap{margin:8px 0}
.cs-live-region{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0)}
.cs-skel{height:120px;border-radius:var(--radius-lg)}

/* inside dialogs (portaled outside .cs-root) */
/* the host clips a long title to one line; the session name must stay readable */
.cs-dlg-title{white-space:normal;overflow-wrap:anywhere}
.cs-select{width:100%}
.cs-lead{margin:0 0 8px;font-size:13px;overflow-wrap:anywhere}
.cs-small{display:block;font-size:12px;color:var(--muted)}
.cs-bad{display:block;margin-top:4px;font-size:12px;color:var(--danger-text)}
.cs-field{display:block;margin:12px 0 4px;font-weight:600;font-size:13px}
.cs-checkrow{display:flex;gap:8px;align-items:center;margin:12px 0 0;font-size:13px}
.cs-check{width:16px;height:16px;margin:0;flex:none;accent-color:var(--accent)}
.cs-list{list-style:none;margin:8px 0;padding:0}
.cs-list li{margin:8px 0}
.cs-list .cs-checkrow{align-items:flex-start;margin:0}
.cs-risk{margin:8px 0}
.cs-risk>p{margin:0 0 8px;font-size:13px;overflow-wrap:anywhere}
.cs-stack{display:flex;flex-direction:column;gap:8px}
.cs-actions{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:8px;flex:1}
.cs-push{margin-right:auto}

/* narrow */
@media (max-width:640px){
  .cs-grid{grid-template-columns:minmax(0,1fr)}
  .cs-tiers{flex-wrap:nowrap;overflow-x:auto;padding:4px;-webkit-overflow-scrolling:touch}
  .cs-actions>*{flex:1 1 auto}
}
@media (max-width:640px),(pointer:coarse){
  .cs-btn,.cs-disc summary,.cs-check{min-height:44px}
  .cs-input,.cs-textarea{min-height:44px}
}
`;
const TIER_KEY = "cao-shelf:tiers";
const DEFAULT_TIERS = { needs_you: true, live: true, parked: true, stale: false };
function loadTiers() {
  try {
    const raw = JSON.parse(localStorage.getItem(TIER_KEY) ?? "null");
    if (raw && typeof raw === "object") {
      return {
        needs_you: raw.needs_you ?? DEFAULT_TIERS.needs_you,
        live: raw.live ?? DEFAULT_TIERS.live,
        parked: raw.parked ?? DEFAULT_TIERS.parked,
        stale: raw.stale ?? DEFAULT_TIERS.stale
      };
    }
  } catch {
  }
  return DEFAULT_TIERS;
}
function caoProblem(d) {
  if (d.cao.reachable) return null;
  const url = d.cao.base_url;
  switch (d.cao.error?.code) {
    case "cao_auth_required":
      return { kind: "auth", text: "CAO answered 401 or 403: it has authentication on. This version of the CAO Shelf cannot sign in to an auth-enabled CAO." };
    case "cao_incompatible":
      return { kind: "incompatible", text: `CAO at ${url} answered in a shape this version does not understand: ${d.cao.error.message}` };
    case "cao_error":
      return { kind: "error", text: `CAO at ${url} returned an error: ${d.cao.error.message}` };
    case "forbidden_host":
      return { kind: "address", text: `The CAO address ${url} is not allowed. Only http on 127.0.0.1, localhost or ::1 works. Change it in Settings.` };
    default:
      return { kind: "unreachable", text: `CAO is not reachable at ${url}.` };
  }
}
function useClock(ms) {
  const [t, setT] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setT(Date.now()), ms);
    return () => clearInterval(id);
  }, [ms]);
  return t;
}
const CLOCK_MS = 15e3;
const REASON_ID = "cs-reason";
function App() {
  const api = useShelfApi();
  const { data, receivedAt, error, loading, refresh } = useShelf();
  const tickMs = useClock(CLOCK_MS);
  const { active } = useAppInfo();
  const setBadge = useNavBadge();
  const [tiers, setTiers] = useState(loadTiers);
  const [message, setMessage] = useState("");
  const [stop, setStop] = useState(null);
  const [start, setStart] = useState(false);
  const [remote, setRemote] = useState(null);
  const [allHistory, setAllHistory] = useState(false);
  const [settings, setSettings] = useState(false);
  const prevNeeds = useRef(null);
  const toggle = (k) => setTiers((t) => {
    const next = { ...t, [k]: !t[k] };
    try {
      localStorage.setItem(TIER_KEY, JSON.stringify(next));
    } catch {
    }
    return next;
  });
  const announce = useCallback((m) => setMessage(m), []);
  const needs = data?.counts.by_tier.needs_you;
  useEffect(() => {
    if (needs !== void 0) setBadge(needs);
  }, [needs, setBadge]);
  useEffect(() => {
    if (!data) return;
    const now2 = new Set(data.sessions.filter((s) => s.tier === "needs_you").map((s) => s.name));
    if (prevNeeds.current) {
      const fresh = [...now2].filter((n) => !prevNeeds.current.has(n));
      if (fresh.length > 0) setMessage(`${fresh.join(", ")} needs you`);
    }
    prevNeeds.current = now2;
  }, [data]);
  const problem = data ? caoProblem(data) : null;
  const noSnapshot = !!data && !data.cao.reachable && !data.stale_snapshot;
  const backendDown = !!error;
  const disabledReason = backendDown ? "The CAO Shelf backend is not reachable." : problem ? "CAO is not reachable." : null;
  const problemBanner = !!problem && !!data && !noSnapshot;
  const backendBanner = backendDown && !!data;
  const reasonId = disabledReason && (problemBanner || backendBanner) ? REASON_ID : void 0;
  const generatedAt = data?.generated_at;
  const now = useMemo(() => {
    if (!generatedAt) return new Date(tickMs).toISOString();
    const base = Date.parse(generatedAt);
    return new Date(Math.max(base, tickMs - (receivedAt - base))).toISOString();
  }, [generatedAt, receivedAt, tickMs]);
  const ctx = useMemo(
    () => ({ api, disabledReason, reasonId, announce, refresh, now }),
    [api, disabledReason, reasonId, announce, refresh, now]
  );
  const grouped = useMemo(() => {
    const g = { needs_you: [], live: [], parked: [], stale: [] };
    for (const s of data?.sessions ?? []) g[s.tier].push(s);
    return g;
  }, [data]);
  const subtitle = data && !noSnapshot ? `${data.counts.sessions} CAO session${data.counts.sessions === 1 ? "" : "s"} at a glance: what needs you, what is working, what is parked.` : "Your CAO sessions at a glance: what needs you, what is working, what is parked.";
  const hidden = data?.counts.hidden_sessions ?? 0;
  const hiddenNote = hidden > 0 ? /* @__PURE__ */ jsxs("p", { className: "cs-note", role: "status", children: [
    hidden,
    " session",
    hidden === 1 ? "" : "s",
    " hidden (unsupported name). The shelf can only address names of letters, digits, - and _ (up to 59 characters), so it cannot show, answer or stop ",
    hidden === 1 ? "it" : "them",
    ". Use CAO for ",
    hidden === 1 ? "it" : "them",
    "."
  ] }) : null;
  const stopFromCard = (name) => setStop({ scope: "named", name });
  const remoteSession = remote ? data?.sessions.find((x) => x.name === remote.name) ?? remote : null;
  const zeroSessions = !!data && !noSnapshot && data.sessions.length === 0;
  const startButton = /* @__PURE__ */ jsx(Button, { primary: true, onClick: () => setStart(true), disabled: !!disabledReason, title: disabledReason ?? void 0, "aria-describedby": reasonId, children: "Start session" });
  const header = /* @__PURE__ */ jsx(PageHeader, { title: "CAO Shelf", subtitle, actions: zeroSessions ? void 0 : startButton });
  let body;
  if (!data && loading) {
    body = /* @__PURE__ */ jsx("div", { className: "cs-grid", "aria-busy": "true", "aria-label": "Loading sessions", children: [0, 1, 2].map((i) => /* @__PURE__ */ jsx(Skeleton, { className: "cs-skel" }, i)) });
  } else if (!data) {
    body = /* @__PURE__ */ jsx(
      EmptyState,
      {
        icon: null,
        title: "Cannot reach the CAO Shelf backend.",
        subtitle: `${error?.message ?? "No answer yet."} It starts with Kiro Crew; check that the app is enabled and trusted.`,
        action: /* @__PURE__ */ jsx(Button, { primary: true, onClick: refresh, children: "Try again" })
      }
    );
  } else if (noSnapshot) {
    body = /* @__PURE__ */ jsx(
      EmptyState,
      {
        icon: null,
        title: problem?.kind === "auth" ? "This CAO needs a sign-in." : "Cannot reach CAO.",
        subtitle: problem?.kind === "unreachable" ? `Cannot reach CAO at ${data.cao.base_url}. Start it with cao-server, or change the address in Settings.` : problem?.text,
        action: /* @__PURE__ */ jsx(Button, { primary: true, onClick: () => setSettings(true), children: "Open Settings" })
      }
    );
  } else if (data.sessions.length === 0) {
    body = /* @__PURE__ */ jsxs(Fragment, { children: [
      /* @__PURE__ */ jsx(EmptyState, { icon: null, title: "No CAO sessions yet.", subtitle: "Start one and it shows up here as a card.", action: startButton }),
      hiddenNote
    ] });
  } else {
    const dim = !!problem || backendDown;
    body = /* @__PURE__ */ jsxs("div", { className: dim ? "cs-dim" : void 0, children: [
      /* @__PURE__ */ jsx(TierToggles, { data, visible: tiers, onToggle: toggle }),
      /* @__PURE__ */ jsx(HonestLine, { data }),
      hiddenNote,
      [
        ["needs_you", "Needs you", "Nothing needs you."],
        ["live", "Live now", "Nothing is working."],
        ["parked", `Parked, idle under ${hours(data.thresholds.stale_hours)} h`, void 0],
        ["stale", `Stale, over ${hours(data.thresholds.stale_hours)} h, dimmed`, void 0]
      ].map(
        ([k, title, empty]) => tiers[k] ? /* @__PURE__ */ jsx(Section, { title, count: grouped[k].length, empty, children: grouped[k].map((s) => /* @__PURE__ */ jsx(SessionCard, { s, onStop: stopFromCard, onRemote: setRemote }, s.name)) }, k) : null
      )
    ] });
  }
  const staleCount = data?.counts.by_tier.stale ?? 0;
  return /* @__PURE__ */ jsxs(ShelfContext.Provider, { value: ctx, children: [
    /* @__PURE__ */ jsx("style", { "data-cao-shelf": "", children: CSS }),
    header,
    /* @__PURE__ */ jsxs("div", { className: "cs-root cs-page px-4 md:px-6 pb-8 overflow-y-auto flex-1 min-h-0", "data-active": active === false ? "false" : "true", children: [
      problemBanner && problem && data ? (
        // a problem banner implies a stale snapshot: without one the page shows the empty state instead
        /* @__PURE__ */ jsx(
          ErrorNotice,
          {
            id: backendBanner ? void 0 : REASON_ID,
            className: "cs-gap",
            title: problem.text,
            message: `Showing the last snapshot, ${fmtAge(ageSince(data.last_good_at ?? data.generated_at, now))} old. Actions are off until CAO answers.`
          }
        )
      ) : null,
      backendBanner && data ? /* @__PURE__ */ jsx(
        ErrorNotice,
        {
          id: REASON_ID,
          className: "cs-gap",
          title: "Lost contact with the CAO Shelf backend.",
          message: `Showing the last snapshot, ${fmtAge(ageSince(data.last_good_at ?? data.generated_at, now))} old. Actions are off until it answers.`
        }
      ) : null,
      /* @__PURE__ */ jsx(RemoteControlIntro, {}),
      settings ? /* @__PURE__ */ jsx(Settings, { onClose: () => setSettings(false) }) : null,
      body,
      data && !noSnapshot && data.sessions.length > 0 ? /* @__PURE__ */ jsxs("div", { className: "cs-foot", children: [
        /* @__PURE__ */ jsxs(Button, { danger: true, onClick: () => setStop({ scope: "stale" }), disabled: !!disabledReason || staleCount === 0, title: disabledReason ?? void 0, "aria-describedby": reasonId, children: [
          "Clean up stale (",
          staleCount,
          ")..."
        ] }),
        /* @__PURE__ */ jsxs(Button, { "aria-expanded": allHistory, onClick: () => setAllHistory((a) => !a), children: [
          "All history",
          data.history_total === null ? "" : ` (${data.history_total} closed helpers)`
        ] }),
        /* @__PURE__ */ jsx(Button, { "aria-expanded": settings, onClick: () => setSettings((s) => !s), children: "Settings" }),
        /* @__PURE__ */ jsx("span", { children: "Cleanup always asks first and starts with nothing ticked." })
      ] }) : null,
      allHistory && data ? /* @__PURE__ */ jsx(AllHistory, {}) : null,
      data && !noSnapshot && data.sessions.length === 0 && !settings ? /* @__PURE__ */ jsx("div", { className: "cs-foot", children: /* @__PURE__ */ jsx(Button, { onClick: () => setSettings(true), children: "Settings" }) }) : null,
      /* @__PURE__ */ jsx("div", { className: "cs-live-region", role: "status", "aria-live": "polite", "data-testid": "live-region", children: message }),
      stop ? /* @__PURE__ */ jsx(StopDialog, { target: stop, onClose: () => setStop(null) }) : null,
      start ? /* @__PURE__ */ jsx(StartDialog, { onClose: () => setStart(false) }) : null,
      remoteSession ? /* @__PURE__ */ jsx(RelayDialog, { s: remoteSession, onClose: () => setRemote(null) }) : null
    ] })
  ] });
}
export {
  App as default
};
