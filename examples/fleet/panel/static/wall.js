import { renderScreen } from "./screen.js";
import { createPoller } from "./poller.js";

const TILE_FONT = '12px "JetBrains Mono", ui-monospace, monospace';
const TILE_LINE_H = 16;

// status derives from a session's terminal status when known, else machine state
export function tileStatus(machineOnline, termStatus) {
  if (!machineOnline) return "offline";
  const s = (termStatus || "").toLowerCase();
  if (s.includes("process") || s.includes("run") || s.includes("work")) return "working";
  if (s.includes("error")) return "error";
  return "idle";
}

// Build one tile element + its poller. `onOpen(machine, session)` opens console.
export function buildTile({ machine, machineOnline, session, onOpen }) {
  const id = session.id || session.name;
  const el = document.createElement("div");
  el.className = "tile";
  el.tabIndex = 0;
  el.setAttribute("role", "button");
  el.setAttribute("aria-label", `Open ${id} on ${machine}`);

  const head = document.createElement("div");
  head.className = "tile-head";
  const dot = document.createElement("span");
  dot.className = "dot";
  const title = document.createElement("span");
  title.className = "tile-title";
  title.textContent = `${machine} · ${id}`;
  const statusText = document.createElement("span");
  statusText.className = "tile-status";
  head.append(dot, title, statusText);

  const meta = document.createElement("div");
  meta.className = "tile-meta muted";

  const body = document.createElement("div");
  body.className = "tile-screen";
  body.textContent = "connecting…";

  el.append(head, meta, body);
  enrichMeta(machine, id, meta);

  let status = tileStatus(machineOnline, session.status);
  const applyStatus = (s) => {
    status = s;
    el.dataset.status = s;
    statusText.textContent = s;
  };
  applyStatus(status);

  let screenMisses = 0; // tolerate a single dropped frame before showing "offline"
  const poller = createPoller({
    machine,
    session: id,
    getState: () => ({ focused: false, status }),
    onFrame: (frame, meta) => {
      screenMisses = 0;
      renderScreen(body, frame, {
        font: TILE_FONT, lineHeight: TILE_LINE_H,
        maxWidth: body.clientWidth || 300, follow: "always",
      });
      el.classList.toggle("fallback", meta.fallback);
      // flash "changed" cue for non-focused updates
      el.classList.remove("pulse"); void el.offsetWidth; el.classList.add("pulse");
    },
    // one transient miss keeps the last frame/status; only sustained misses flip offline
    onError: () => { if (++screenMisses >= 2) { body.textContent = "unreachable"; applyStatus("offline"); } },
  });

  const open = () => onOpen(machine, id);
  el.addEventListener("click", open);
  el.addEventListener("keydown", (e) => { if (e.key === "Enter") open(); });

  if (machineOnline) poller.start();
  else applyStatus("offline");

  return {
    el, poller, setStatus: applyStatus,
    // refresh status from fresh fleet data without tearing the tile down
    update(sess, online) { applyStatus(tileStatus(online, sess.status)); },
  };
}

// Fill "provider · working-dir" from the session detail + the cwd endpoint,
// so each tile shows which agent runs in which folder.
async function enrichMeta(machine, session, el) {
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
    if (parts.length) el.textContent = parts.join(" · ");
  } catch {}
}
