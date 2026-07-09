import { buildTile } from "./wall.js";
import { openConsole } from "./console.js";
import { dampFleet } from "./fleet.js";

const $ = (s, r = document) => r.querySelector(s);

// Build an <option> with textContent (never innerHTML): value/label may come from
// a remote node's providers/profiles or the registry, so treat them as untrusted.
function opt(value, label) {
  const o = document.createElement("option");
  o.value = value;
  o.textContent = label == null ? value : label;
  return o;
}
// Replace a <select>'s options from [{value,label}] without parsing HTML.
function setOptions(sel, items) {
  sel.replaceChildren(...items.map((i) => opt(i.value, i.label)));
}

const wallEl = $("#wall");
const statusLine = $("#status-line");
const tileMap = new Map(); // `${machine}::${session}` -> tile { el, poller, update }
const emptyMap = new Map(); // machine name -> "no sessions" card element
let consoleHandle = null;
let lastFleet = { machines: [] };
const fleetState = new Map(); // per-machine health debounce (see fleet.js)

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

function onOpen(machine, session) {
  if (consoleHandle) consoleHandle.close();
  consoleHandle = openConsole({ machine, session, onClosed: () => { consoleHandle = null; } });
}

async function refresh() {
  let data;
  try { data = await api("/api/fleet"); }
  catch (e) { statusLine.textContent = "fleet error: " + e.message; return; }
  // Hold each node's last-known-good state through a transient probe miss so a
  // jittery/relayed node doesn't flap offline↔idle every poll.
  const machines = dampFleet(data.machines, fleetState);
  lastFleet = { ...data, machines };

  // Reconcile against the live set instead of rebuilding. Existing tiles (and
  // their pollers) are kept streaming, so the wall never flashes "connecting…".
  const desired = new Map(); // key -> { machine, machineOnline, session }
  const desiredEmpty = new Set(); // machine names that should show a "no sessions" card
  const order = []; // desired DOM order: { kind: "tile"|"empty", key }
  let online = 0, sessions = 0;

  for (const m of machines) {
    if (m.online) online++;
    const list = m.sessions || [];
    sessions += list.length;
    for (const s of list) {
      const key = `${m.name}::${s.id || s.name}`;
      desired.set(key, { machine: m.name, machineOnline: m.online, session: s });
      order.push({ kind: "tile", key });
    }
    if (m.online && list.length === 0) {
      desiredEmpty.add(m.name);
      order.push({ kind: "empty", key: m.name });
    }
  }

  let changed = false;
  // remove tiles whose session is gone
  for (const [key, tile] of tileMap) {
    if (!desired.has(key)) { tile.poller.stop(); tile.poller.forget(); tile.el.remove(); tileMap.delete(key); changed = true; }
  }
  // remove empty cards no longer needed
  for (const [name, el] of emptyMap) {
    if (!desiredEmpty.has(name)) { el.remove(); emptyMap.delete(name); changed = true; }
  }
  // add new tiles; update status on kept ones WITHOUT tearing them down
  for (const [key, d] of desired) {
    const tile = tileMap.get(key);
    if (!tile) {
      tileMap.set(key, buildTile({ machine: d.machine, machineOnline: d.machineOnline, session: d.session, onOpen }));
      changed = true;
    } else if (tile.update) {
      tile.update(d.session, d.machineOnline);
    }
  }
  // add new empty cards
  for (const name of desiredEmpty) {
    if (!emptyMap.has(name)) {
      const el = document.createElement("div");
      el.className = "tile empty";
      const head = document.createElement("div");
      head.className = "tile-head";
      const dot = document.createElement("span");
      dot.className = "dot";
      const t = document.createElement("span");
      t.className = "tile-title";
      t.textContent = name; // machine name from the registry — set as text, never HTML
      head.append(dot, t);
      const scr = document.createElement("div");
      scr.className = "tile-screen muted";
      scr.textContent = "no sessions";
      el.append(head, scr);
      emptyMap.set(name, el);
      changed = true;
    }
  }
  // only touch the DOM order when membership actually changed (steady state = no churn)
  if (changed) {
    for (const item of order) {
      const el = item.kind === "tile" ? tileMap.get(item.key)?.el : emptyMap.get(item.key);
      if (el) wallEl.appendChild(el);
    }
  }

  statusLine.textContent = `${online}/${machines.length} nodes online · ${sessions} sessions · ${new Date().toLocaleTimeString()}`;
}

document.addEventListener("visibilitychange", () => {
  // pause tile pollers while the tab is hidden; resume on return
  if (document.hidden) for (const t of tileMap.values()) t.poller.stop();
  else for (const t of tileMap.values()) t.poller.start();
});

// ---- Launch modal (pick node + folder + provider + agent, then cao launch) ----
const launchDialog = $("#launch-dialog");
const launchForm = $("#launch-form");
const launchMsg = $("#launch-msg");

$("#launch-btn").addEventListener("click", openLaunch);

async function openLaunch() {
  const nodeSel = $("#launch-node");
  const online = lastFleet.machines.filter((m) => m.online);
  if (!online.length) { alert("No online nodes to launch on."); return; }
  setOptions(nodeSel, online.map((m) => ({ value: m.name, label: `${m.name} (${m.label || m.host})` })));
  launchForm.reset();
  launchMsg.textContent = "";
  await loadNodeOptions(online[0].name);
  launchDialog.showModal();
}

$("#launch-node").addEventListener("change", (e) => loadNodeOptions(e.target.value));

// Populate provider + profile dropdowns from the selected node.
async function loadNodeOptions(node) {
  const provSel = $("#launch-provider");
  const profSel = $("#launch-profile");
  setOptions(provSel, [{ value: "", label: "loading…" }]);
  setOptions(profSel, [{ value: "", label: "loading…" }]);
  try {
    const [provs, profs] = await Promise.all([
      api(`/api/machines/${node}/providers`),
      api(`/api/machines/${node}/profiles`),
    ]);
    const installed = provs.filter((p) => p.installed);
    const provList = installed.length ? installed : provs;
    setOptions(provSel, provList.map((p) => ({
      value: p.name, label: `${p.name}${p.installed ? "" : " (not installed)"}`,
    })));
    setOptions(profSel, profs.map((p) => {
      const n = typeof p === "string" ? p : p.name;
      return { value: n, label: n };
    }));
  } catch (e) {
    setOptions(provSel, [{ value: "claude_code", label: "claude_code" }]);
    setOptions(profSel, [{ value: "developer", label: "developer" }]);
  }
}

launchForm.addEventListener("submit", async (e) => {
  if (e.submitter && e.submitter.value === "cancel") return; // let the dialog close
  e.preventDefault();
  const f = e.target;
  const node = f.node.value;
  const body = {
    working_directory: f.working_directory.value.trim() || undefined,
    provider: f.provider.value,
    agent_profile: f.agent_profile.value,
    task: f.task.value.trim() || undefined,
  };
  const ok = f.querySelector('button[value="ok"]');
  ok.disabled = true;
  launchMsg.textContent = `launching on ${node}… (agent init can take up to ~90s)`;
  try {
    const r = await api(`/api/machines/${node}/launch`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
    });
    launchDialog.close();
    statusLine.textContent = `launched ${r.session_name} on ${node}`;
    refresh();
  } catch (err) {
    launchMsg.textContent = "launch failed: " + err.message;
  } finally {
    ok.disabled = false;
  }
});

refresh();
setInterval(refresh, 8000); // re-aggregate topology; screens stream via pollers
