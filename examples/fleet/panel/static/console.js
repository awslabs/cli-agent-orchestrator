import { renderScreen } from "./screen.js";
import { createPoller } from "./poller.js";

const CONSOLE_FONT = '13px "JetBrains Mono", ui-monospace, monospace';
const CONSOLE_LINE_H = 18;

const KEY_MAP = {
  interrupt: "C-c", enter: "Enter", esc: "Escape",
  yes: "y", no: "n", up: "Up", down: "Down",
};

async function post(path, body) {
  const r = await fetch(path, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

// Open the focused console overlay for one session. Returns { close }.
export function openConsole({ machine, session, onClosed }) {
  const overlay = document.createElement("div");
  overlay.className = "console-overlay";
  // NOTE: machine comes from the registry and session from a remote node's
  // /sessions — both are untrusted, so keep them OUT of the innerHTML template
  // and set them via textContent/setAttribute below to avoid DOM injection.
  overlay.innerHTML = `
    <div class="console" role="dialog">
      <div class="console-head">
        <button class="back" aria-label="Back to wall">‹ wall</button>
        <span class="dot"></span>
        <span class="console-title"></span>
        <span class="console-status"></span>
        <span class="console-meta muted"></span>
        <span class="spacer"></span>
        <button class="danger shutdown">Shutdown</button>
      </div>
      <div class="console-screen" aria-live="off"></div>
      <div class="quickkeys">
        <button class="qk danger" data-key="interrupt" aria-label="Interrupt (Ctrl-C)">^C</button>
        <button class="qk" data-key="enter" aria-label="Send Enter">⏎</button>
        <button class="qk" data-key="esc" aria-label="Send Escape">Esc</button>
        <button class="qk" data-key="yes">Y</button>
        <button class="qk" data-key="no">N</button>
        <button class="qk" data-key="up" aria-label="Arrow up">↑</button>
        <button class="qk" data-key="down" aria-label="Arrow down">↓</button>
      </div>
      <form class="input-bar">
        <span class="prompt">›</span>
        <textarea rows="1" placeholder="message this agent…  (Enter sends · Shift+Enter newline)"></textarea>
        <button type="submit" class="primary send">Send</button>
      </form>
      <div class="hint">Enter sends a message · ^C interrupts the CLI</div>`;

  overlay.querySelector(".console").setAttribute("aria-label", `Console for ${session} on ${machine}`);
  overlay.querySelector(".console-title").textContent = `${machine} · ${session}`;

  const screenEl = overlay.querySelector(".console-screen");
  const statusEl = overlay.querySelector(".console-status");
  const textarea = overlay.querySelector("textarea");
  const form = overlay.querySelector(".input-bar");
  let status = "idle";
  let lastSent = "";

  const setStatus = (s) => { status = s; statusEl.textContent = s; overlay.querySelector(".console").dataset.status = s; };
  setStatus("idle");

  // show "provider · working-dir" in the header
  (async () => {
    try {
      const d = await fetch(`/api/machines/${machine}/sessions/${session}`).then((r) => r.json());
      const t = (d.terminals || [])[0] || {};
      let wd = "";
      if (t.id) {
        try {
          const w = await fetch(`/api/machines/${machine}/terminals/${t.id}/working-directory`).then((r) => r.json());
          wd = w.working_directory || "";
        } catch {}
      }
      const parts = [t.provider, wd].filter(Boolean);
      if (parts.length) overlay.querySelector(".console-meta").textContent = parts.join(" · ");
    } catch {}
  })();

  const poller = createPoller({
    machine, session,
    getState: () => ({ focused: true, status }),
    onFrame: (frame) => {
      if (status === "offline") setStatus("idle");
      renderScreen(screenEl, frame, {
        font: CONSOLE_FONT, lineHeight: CONSOLE_LINE_H,
        maxWidth: screenEl.clientWidth || 700, follow: "smart",
      });
    },
    onError: () => setStatus("offline"),
  });
  poller.start();

  function close() { poller.stop(); overlay.remove(); document.removeEventListener("keydown", onKey); if (onClosed) onClosed(); }
  function onKey(e) { if (e.key === "Escape" && document.activeElement !== textarea) close(); }
  document.addEventListener("keydown", onKey);

  overlay.querySelector(".back").addEventListener("click", close);
  overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) close(); });

  overlay.querySelector(".shutdown").addEventListener("click", async (e) => {
    e.target.disabled = true;
    try { await post(`/api/machines/${machine}/sessions/${session}/shutdown`); close(); }
    catch (err) { alert(`shutdown failed: ${err.message}`); e.target.disabled = false; }
  });

  overlay.querySelectorAll(".qk").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const key = KEY_MAP[btn.dataset.key];
      try {
        const tid = await resolveTid(machine, session);
        if (tid) await post(`/api/machines/${machine}/terminals/${tid}/key`, { key });
      } catch (err) { flash(statusEl, `key failed`); }
    });
  });

  textarea.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
    if (e.key === "ArrowUp" && textarea.value === "") { textarea.value = lastSent; }
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = textarea.value.trim();
    if (!msg) return;
    const send = form.querySelector(".send");
    send.disabled = true;
    try {
      await post(`/api/machines/${machine}/sessions/${session}/send`, { message: msg });
      lastSent = msg; textarea.value = "";
      flash(statusEl, "sent");
    } catch (err) { alert(`send failed: ${err.message}`); }
    finally { send.disabled = false; }
  });

  document.body.appendChild(overlay);
  textarea.focus();
  return { close };
}

// small helpers (kept local to the console module)
const _tidCache = new Map();
async function resolveTid(machine, session) {
  const key = `${machine}::${session}`;
  if (_tidCache.has(key)) return _tidCache.get(key);
  const r = await fetch(`/api/machines/${machine}/sessions/${session}`);
  const d = await r.json();
  const tid = (d.terminals || [])[0]?.id || null;
  if (tid) _tidCache.set(key, tid);
  return tid;
}
function flash(el, text) { const t = el.textContent; el.textContent = text; setTimeout(() => { el.textContent = t; }, 1200); }
