// RFC 6902 JSON Patch — pure-JS apply + diff for the delta-sync client.
//
// No JIT: this file uses only plain object/array operations (no `eval`,
// `new Function`, etc.), so the single-file bundle runs under the spec's
// no-`unsafe-eval` CSP.
//
// `clientDiff` mirrors the server's `ui_state_service.diff_snapshot` granularity
// (whole-key replace for `terminals`/`sessions`, per-key replace for nested
// scalars) so that `applyPatch(prev, clientDiff(prev, curr))` deep-equals `curr`
// (RFC-6902 round-trip).

import type { JsonPatchOp } from "./types";

/** Top-level snapshot keys replaced wholesale (never element-diffed). */
const WHOLE_KEY_REPLACE = new Set(["terminals", "sessions"]);

type Json = unknown;

/** Escape a single JSON Pointer reference token (RFC 6901 §3). */
export function escapeToken(token: string): string {
  return token.replace(/~/g, "~0").replace(/\//g, "~1");
}

/** Reverse JSON Pointer escaping (`~1` -> `/`, `~0` -> `~`). */
export function unescapeToken(token: string): string {
  return token.replace(/~1/g, "/").replace(/~0/g, "~");
}

/** Parse a JSON Pointer string into its decoded reference tokens. */
export function parsePointer(pointer: string): string[] {
  if (pointer === "") return [];
  if (pointer[0] !== "/") {
    throw new Error(`invalid JSON pointer: ${pointer}`);
  }
  return pointer.slice(1).split("/").map(unescapeToken);
}

function isObject(value: Json): value is Record<string, Json> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function deepClone<T>(value: T): T {
  // structuredClone is available in all target runtimes; fall back to JSON for
  // older happy-dom/node. Both avoid any code generation (JIT-free).
  if (typeof structuredClone === "function") return structuredClone(value);
  return JSON.parse(JSON.stringify(value)) as T;
}

/** Deep structural equality for JSON values. */
export function deepEqual(a: Json, b: Json): boolean {
  if (a === b) return true;
  if (typeof a !== typeof b) return false;
  if (Array.isArray(a) && Array.isArray(b)) {
    if (a.length !== b.length) return false;
    for (let i = 0; i < a.length; i += 1) {
      if (!deepEqual(a[i], b[i])) return false;
    }
    return true;
  }
  if (isObject(a) && isObject(b)) {
    const ak = Object.keys(a);
    const bk = Object.keys(b);
    if (ak.length !== bk.length) return false;
    for (const k of ak) {
      if (!Object.prototype.hasOwnProperty.call(b, k)) return false;
      if (!deepEqual(a[k], b[k])) return false;
    }
    return true;
  }
  return false;
}

function getAtPointer(doc: Json, tokens: string[]): Json {
  let cur: Json = doc;
  for (const token of tokens) {
    if (Array.isArray(cur)) {
      cur = cur[Number(token)];
    } else if (isObject(cur)) {
      cur = cur[token];
    } else {
      throw new Error(`pointer traverses a non-container at '${token}'`);
    }
  }
  return cur;
}

function setChild(
  parent: Json,
  token: string,
  value: Json,
  isAdd: boolean,
): void {
  if (Array.isArray(parent)) {
    if (token === "-") {
      parent.push(value);
      return;
    }
    const idx = Number(token);
    if (isAdd) parent.splice(idx, 0, value);
    else parent[idx] = value;
    return;
  }
  if (isObject(parent)) {
    parent[token] = value;
    return;
  }
  throw new Error(`cannot set child '${token}' on a non-container`);
}

function removeChild(parent: Json, token: string): void {
  if (Array.isArray(parent)) {
    parent.splice(Number(token), 1);
    return;
  }
  if (isObject(parent)) {
    delete parent[token];
    return;
  }
  throw new Error(`cannot remove child '${token}' from a non-container`);
}

/**
 * Apply an RFC 6902 patch to `doc`, returning a new document (input untouched).
 * Supports add / remove / replace / move / copy / test.
 */
export function applyPatch(doc: Json, ops: JsonPatchOp[]): Json {
  let result = deepClone(doc);
  for (const op of ops) {
    const tokens = parsePointer(op.path);
    if (op.op === "add" || op.op === "replace") {
      if (tokens.length === 0) {
        result = deepClone(op.value);
        continue;
      }
      const parent = getAtPointer(result, tokens.slice(0, -1));
      setChild(
        parent,
        tokens[tokens.length - 1],
        deepClone(op.value),
        op.op === "add",
      );
    } else if (op.op === "remove") {
      const parent = getAtPointer(result, tokens.slice(0, -1));
      removeChild(parent, tokens[tokens.length - 1]);
    } else if (op.op === "move" || op.op === "copy") {
      const fromTokens = parsePointer(op.from ?? "");
      const moved = deepClone(getAtPointer(result, fromTokens));
      if (op.op === "move") {
        const fromParent = getAtPointer(result, fromTokens.slice(0, -1));
        removeChild(fromParent, fromTokens[fromTokens.length - 1]);
      }
      const parent = getAtPointer(result, tokens.slice(0, -1));
      setChild(parent, tokens[tokens.length - 1], moved, true);
    } else if (op.op === "test") {
      const actual = getAtPointer(result, tokens);
      if (!deepEqual(actual, op.value as Json)) {
        throw new Error(`test op failed at ${op.path}`);
      }
    } else {
      throw new Error(`unsupported op: ${(op as JsonPatchOp).op}`);
    }
  }
  return result;
}

function diffObject(
  prev: Record<string, Json>,
  curr: Record<string, Json>,
  base: string,
): JsonPatchOp[] {
  const ops: JsonPatchOp[] = [];
  const keys = new Set([...Object.keys(prev), ...Object.keys(curr)]);
  for (const key of Array.from(keys).sort()) {
    const pointer = `${base}/${escapeToken(key)}`;
    const inPrev = Object.prototype.hasOwnProperty.call(prev, key);
    const inCurr = Object.prototype.hasOwnProperty.call(curr, key);
    if (inPrev && !inCurr) {
      ops.push({ op: "remove", path: pointer });
    } else if (inCurr && !inPrev) {
      ops.push({ op: "add", path: pointer, value: curr[key] });
    } else if (deepEqual(prev[key], curr[key])) {
      continue;
    } else if (base === "" && WHOLE_KEY_REPLACE.has(key)) {
      ops.push({ op: "replace", path: pointer, value: curr[key] });
    } else if (isObject(prev[key]) && isObject(curr[key])) {
      ops.push(
        ...diffObject(
          prev[key] as Record<string, Json>,
          curr[key] as Record<string, Json>,
          pointer,
        ),
      );
    } else {
      ops.push({ op: "replace", path: pointer, value: curr[key] });
    }
  }
  return ops;
}

/**
 * Compute the RFC 6902 patch from `prev` to `curr`, matching the server's
 * `diff_snapshot` granularity. Round-trips: `applyPatch(prev, clientDiff(prev,
 * curr))` deep-equals `curr`.
 */
export function clientDiff(prev: Json, curr: Json): JsonPatchOp[] {
  if (isObject(prev) && isObject(curr)) {
    return diffObject(prev, curr, "");
  }
  // Non-object roots: a single whole-document replace.
  if (deepEqual(prev, curr)) return [];
  return [{ op: "replace", path: "", value: curr }];
}
