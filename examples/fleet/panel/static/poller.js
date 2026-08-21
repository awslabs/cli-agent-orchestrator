import { intervalFor } from "./schedule.js";

async function json(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

// Resolve (and cache) the first terminal id of a session.
const _termCache = new Map(); // key `${machine}::${session}` -> terminal id
async function resolveTerminal(machine, session) {
  const key = `${machine}::${session}`;
  if (_termCache.has(key)) return _termCache.get(key);
  const d = await json(`/api/machines/${machine}/sessions/${session}`);
  const tid = (d.terminals || [])[0]?.id || null;
  if (tid) _termCache.set(key, tid);
  return tid;
}

// Create a self-rescheduling poller for one tile.
// getState() -> { focused, status } drives cadence; onFrame(frame, meta) paints.
export function createPoller({ machine, session, getState, onFrame, onError }) {
  let stopped = false;
  let timer = null;

  async function tick() {
    if (stopped) return;
    const state = getState();
    const wait = intervalFor(state);
    if (wait === 0) { schedule(1500); return; } // offline: recheck slowly
    try {
      const tid = await resolveTerminal(machine, session);
      if (tid) {
        const res = await json(`/api/machines/${machine}/terminals/${tid}/screen`);
        onFrame(res.screen || "", { fallback: !!res.fallback, terminalId: tid });
      }
    } catch (e) {
      if (onError) onError(e);
    }
    schedule(wait);
  }

  function schedule(ms) {
    if (stopped) return;
    clearTimeout(timer);
    timer = setTimeout(tick, ms);
  }

  return {
    start() { stopped = false; tick(); },
    stop() { stopped = true; clearTimeout(timer); },
    forget() { _termCache.delete(`${machine}::${session}`); },
  };
}
