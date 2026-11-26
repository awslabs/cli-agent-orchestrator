// Storage with an in-memory fallback. Pins, selection and filter live in shared(); fold
// heights live in localStorage. Any storage error is caught: the window keeps working with an
// in-memory copy and shows the notice for the rest of the run. getShared, setShared and local
// (getItem/setItem) are injected, so a missing or throwing storage is handled like a full one.
//
// A read goes to storage first, because another block of the window may have changed or cleared the
// key. Memory answers only when storage cannot: a read that throws, or a key whose last write here
// failed (storage still holds an older value than the one this run wrote).
export const STORAGE_NOTICE = "settings are not being saved";

export function createStore({ getShared, setShared, local } = {}) {
  const mem = new Map();
  const unsaved = new Set();       // keys whose latest value exists only in mem
  let degraded = false;
  const hit = (v) => v !== undefined && v !== null;
  const read = (key, load) => {
    if (unsaved.has(key)) return mem.get(key);
    try {
      const v = load();
      return hit(v) ? v : undefined;
    } catch {
      degraded = true;
      return mem.get(key);
    }
  };
  const write = (key, value, save) => {
    mem.set(key, value);
    try { save(); unsaved.delete(key); } catch { degraded = true; unsaved.add(key); }
  };
  return {
    get: (key) => read(key, () => getShared(key)),
    set: (key, value) => write(key, value, () => setShared(key, value)),
    getLocal: (key) => read(`local:${key}`, () => local.getItem(key)),
    setLocal: (key, value) => write(`local:${key}`, String(value), () => local.setItem(key, String(value))),
    degraded: () => degraded,
    notice: () => (degraded ? STORAGE_NOTICE : null),
  };
}
