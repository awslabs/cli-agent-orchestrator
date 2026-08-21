// Debounce a machine's health so one dropped/slow /health probe doesn't flap the
// whole node between "offline" and "idle".
//
// A high-latency or DERP-relayed node (e.g. an intercontinental or NAT'd VPS)
// occasionally misses a single health probe while being perfectly up. The raw
// /api/fleet response recomputes `online` from that one probe, so a single miss
// turns every tile on the node offline until the next poll — the visible flap.
//
// dampFleet holds a node's last-known-good snapshot (online + sessions) for a
// short grace window and only surfaces "offline" after it has been unreachable
// for OFFLINE_GRACE consecutive polls. Pure + framework-free so it unit-tests
// like schedule.js.

export const OFFLINE_GRACE = 2; // consecutive failed probes tolerated before offline

// machines: fresh /api/fleet list.
// state: Map<name, { lastGood, misses }> owned by the caller, persisted across polls.
// Returns a new, damped machines list (same order); marks held nodes { stale: true }.
export function dampFleet(machines, state, grace = OFFLINE_GRACE) {
  return machines.map((m) => {
    const prev = state.get(m.name) || { lastGood: null, misses: 0 };

    if (m.online) {
      state.set(m.name, { lastGood: m, misses: 0 });
      return m;
    }

    // Probe failed this round.
    const misses = prev.misses + 1;
    state.set(m.name, { lastGood: prev.lastGood, misses });

    if (prev.lastGood && misses <= grace) {
      // Hold last-known-good so tiles keep streaming; flag stale for a subtle cue.
      return { ...prev.lastGood, online: true, stale: true, error: m.error };
    }
    return { ...m, online: false, stale: false };
  });
}
