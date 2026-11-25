// The view models of the left sidebar's two blocks (design D25, D26 and D37): one line per session in the
// Sessions block, and the agent tree and header of the selected session in the Agents block. Pure, so
// `node lib/agents.test.mjs` runs it; the mods only draw what these return. A session row is a letter badge, a name,
// the state mark and, when it has one, an extra mark; an agent row is the logo of its program, a name, an age, the state mark and, for a worker, a pin
// (the lists of Herdr's own window, copied). The state is the mark; its words and the other facts are the row's `tip`, the plain text
// of its tooltip. No function mutates its arguments. Top-level names are unique across lib/*.js (bundle mode, D8), so every helper here is
// prefixed `ag`. Source stays ASCII: the middle dot is written as an escape.
import { HONESTY } from "./honesty.js";
import { cronHuman } from "./flows.js";
import { ageWords, askingKind, contextPercent, displayState, flowOfSession, olderFirst, parseRecap, rowShowsReplied, seenState, sessionTree, statusShape, tabsFor, waitingCount } from "./logic.js";

// ---------- names, words and ages (D37) ----------

const agText = (v) => (typeof v === "string" && v.trim() !== "" ? v.trim() : undefined);

// The name an agent goes by in the left sidebar and in the top bar: the name of the Herdr tab CAO gives its
// terminal (the backend names each tab after the terminal's `cao.window` label, for example `conductor-774e`),
// else its profile, else, as a last resort, a short id. A tab name that is the terminal's own id is the raw id,
// not a name, so it never stands while a profile exists.
export function agentName(t) {
  const id = typeof t?.id === "string" ? t.id : "";
  const tab = agText(t?.window);
  if (tab !== undefined && tab !== id) return tab;
  return agText(t?.profile) ?? id.slice(0, 8);
}

// The compact age of a row: "now" under a minute, then `12m`, `3h`, `2d`. null for what is no time.
export function ageShort(ms) {
  if (typeof ms !== "number" || !Number.isFinite(ms) || ms < 0) return null;
  const m = Math.floor(ms / 60_000);
  if (m < 1) return "now";
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return h < 24 ? `${h}h` : `${Math.floor(h / 24)}d`;
}

// The words of a display state, as Herdr says them: a blocked agent "needs you", with the kind of what it asks when Herdr reports one
// ("needs you: permission"); an agent that waits on its workers (D38) says how many.
function agWord(state, t) {
  if (state === "blocked") return askingKind(t) === null ? "needs you" : `needs you: ${askingKind(t)}`;
  if (state !== "waiting") return state;
  const n = waitingCount(t);
  return `waiting on ${n} worker${n === 1 ? "" : "s"}`;
}

// ---------- the session's letter badge (D37) ----------

// The Sessions block is styled like Herdr's own spaces list (mods/spaces/main.js of Herdr 0.1.17). A space there is an 18 px badge
// with the first letter of its name (:502), filled with a colour that is a stored assignment per space and not derived from the name:
// the palette colour used least so far, the first in palette order on a tie (render(), :527-537), shown as the hex of a fixed table
// (MARK, :61), and `ansi6` when none is stored (:455). The window has no Herdr space per session, so the same rule runs over the
// session names and its result is kept under the window's own shared key. Both tables are copied: that mod exports neither.
export const BADGE_PALETTE = ["ansi6", "ansi3", "ansi4", "ansi5", "ansi2", "ansi1"];
const AG_MARK = { ansi6: "#22a99a", ansi3: "#e3814f", ansi4: "#5b8def", ansi5: "#8b7cf6", ansi2: "#3fae6a", ansi1: "#d9649a" };
const AG_DEFAULT_COLOR = "ansi6";
const agColorName = (v) => (typeof v === "string" && v !== "" ? v : undefined);

// The letter of a badge. Herdr takes the first code point of the name, upper-cased ([...name][0]?.toUpperCase() ?? "?", :502). Every CAO
// session name starts with `cao-`, so that rule gave every badge a C (owner review, D39 A): a leading `cao-` (any case) is skipped and the
// first letter or digit after it is taken, upper-cased. A name that is only `cao-`, or has nothing alphanumeric after it, and a name
// without the prefix use Herdr's rule on the whole name.
const AG_PREFIX = /^cao-/i;
const agFirst = (text) => [...text][0]?.toUpperCase();
export function badgeLetter(name) {
  if (typeof name !== "string") return "?";
  const named = AG_PREFIX.test(name) ? /[\p{L}\p{N}]/u.exec(name.slice(4))?.[0] : undefined;
  return (named === undefined ? agFirst(name) : agFirst(named)) ?? "?";
}

// The stored colours (session name -> colour name) and the names of every session now listed -> {colors, changed}. A listed session
// without a colour takes the palette colour with the fewest uses, the first in palette order on a tie, in the order of `names`; a
// colour already stored stays; a name no longer listed loses its colour; a stored value that is not a colour name is replaced. A
// list with no name prunes nothing: a lost connection empties it, and the colours must survive. The input is not changed.
export function badgeColors(stored, names) {
  const have = stored !== null && typeof stored === "object" && !Array.isArray(stored) ? stored : {};
  const listed = (Array.isArray(names) ? names : []).filter((n) => typeof n === "string" && n !== "");
  if (listed.length === 0) return { colors: { ...have }, changed: false };
  const wanted = new Set(listed);
  const colors = new Map(Object.entries(have).filter(([name, c]) => wanted.has(name) && agColorName(c) !== undefined));
  for (const name of listed) {
    if (colors.has(name)) continue;
    const uses = (c) => [...colors.values()].filter((v) => v === c).length;
    colors.set(name, BADGE_PALETTE.reduce((best, c) => (uses(c) < uses(best) ? c : best)));
  }
  const next = Object.fromEntries(colors);
  const changed = colors.size !== Object.keys(have).length || [...colors].some(([name, c]) => have[name] !== c);
  return { colors: next, changed };
}

// A colour name -> what fills the badge: a stored hex as it is, the hex of the table for a palette name, else the theme's token of
// that name (`MARK[color] ?? t[color]`, :456), else the default colour.
export function badgeFill(color, theme) {
  const name = agColorName(color);
  if (name === undefined) return AG_MARK[AG_DEFAULT_COLOR];
  if (name.startsWith("#")) return name;
  if (Object.hasOwn(AG_MARK, name)) return AG_MARK[name];
  const token = theme !== null && typeof theme === "object" && Object.hasOwn(theme, name) ? theme[name] : undefined;
  return agColorName(token) ?? AG_MARK[AG_DEFAULT_COLOR];
}

// ---------- the agent's program icon (D37) ----------

// Herdr's Agents list and its tabs mod draw the icon of the terminal's resolved program (`program.icon`, a path in the home folder
// such as `icons/claude.svg`) by `p.tint ? svg(p.icon) : image(p.icon)` (mods/agents/main.js:78-81, mods/tabs/main.js:120). The app's own
// table maps the agent's reported `app` (`claude-code`, `kiro-cli`, `codex`) to that program, so the window keeps no table: it reads the
// program of the record (termOf) and draws what it names. A record with no usable icon gets Herdr's generic agent glyph, `icons/agent.svg`
// (a tinted bot, the file the home holds for a program of kind "agent"), so every row has a logo and the names stay in one column.
// -> {path, tint}
const AG_FALLBACK_ICON = { path: "icons/agent.svg", tint: true };
export function programIcon(program) {
  const icon = program !== null && typeof program === "object" ? agText(program.icon) : undefined;
  return icon === undefined ? { ...AG_FALLBACK_ICON } : { path: icon, tint: Boolean(program.tint) };
}

// ---------- the session line (D26) ----------

// One session -> what its line in the Sessions block shows, and no agent rows: the name; the status shape of the first talkable
// agent (no state word: the shape is the state); an `extra` mark beside it (D39 D: no pill, no word, only a shape and a colour): "bang",
// Herdr's needs-you square, when any terminal of the session is blocked and the agent's own mark is not already that square, else "dot-hi",
// the done dot in the highlight colour, when a talkable agent has replied and the agent's own mark is not already that dot, else null
// (a recorded draft author session shows its tag and no extra mark); the "stuck?" hint with its label; and `tip`, the
// tooltip, which says the name and the words of the state, "needs you: <kind>" and "replied". ctx.replied(terminal) says whether a talkable agent has replied (the model's predicate); ctx.quiet is
// what quietHint returned for the session, or null; ctx.colors is the stored badge colours (badgeColors), a session with none stored
// has the default colour. `badge` is {letter, color name}: the mod draws it before the name, and the state mark at the right end.
export function sessionLine(session, { replied = () => false, quiet = null, colors = null } = {}) {
  const first = session.talkable[0];
  const blocked = session.draft ? undefined : session.terms.find((t) => t.state === "blocked");
  const anyReplied = !session.draft && session.talkable.some((t) => replied(t) && waitingCount(t) === 0);
  const state = first ? displayState(first, { replied: replied(first) }) : null;
  const shape = state === null ? null : statusShape(state);
  const extra = blocked && shape !== "bang" ? "bang" : !blocked && anyReplied && shape !== "dot-hi" ? "dot-hi" : null;
  const words = [
    state === null ? null : agWord(state, first),
    extra === "bang" ? agWord("blocked", blocked) : null,
    extra === "dot-hi" ? "replied" : null,
  ].filter(Boolean);
  const stored = colors !== null && typeof colors === "object" && Object.hasOwn(colors, session.name) ? agColorName(colors[session.name]) : undefined;
  return {
    name: session.name,
    badge: { letter: badgeLetter(session.name), color: stored ?? AG_DEFAULT_COLOR },
    shape,
    extra,
    draft: session.draft === true,
    hint: quiet ? { text: "stuck?", label: HONESTY.heuristic, terminalId: quiet.terminalId } : null,
    tip: words.length === 0 ? session.name : `${session.name}: ${words.join(", ")}`,
  };
}

// ---------- the agent tree (D26) ----------

// The facts of an agent that its row does not draw, for its tooltip: its state in words and the time in it, its
// turn, "talkable" or "worker", its seen state when it is talkable, and Herdr's status message while it works.
// The time shows only when the caller gives `now`.
function agFacts(t, state, worker, now, local) {
  const age = now !== undefined && t.statusMs !== undefined ? ageWords(now - t.statusMs) : null;
  const msg = t.state === "working" && typeof t.msg === "string" ? t.msg.trim() : "";
  const word = agWord(state, t);
  return [
    age === null ? word : age === "just now" ? `${word}, just now` : `${word} for ${age}`,
    `turn ${t.turn}`,
    worker ? "worker" : "talkable",
    worker ? null : seenState(t, local) && `seen: ${seenState(t, local)}`,
    msg || null,
  ].filter(Boolean).join(", ");
}

// The agents of a session in tree order. Every agent is shown, whatever their number (design D41 A, D42 C): the block is the tree of who
// started whom, and it scrolls when it is taller than the block. A row's parent is the terminal its caller names; its depth is one more
// than its parent's. A row whose caller is not among the session's terminals sits at depth 1 with the note "parent not listed" (that missing
// parent counts as the step above it). Siblings, and the top level, are in creation order, then by id. No session (null, or the stored
// selection of a session the list no longer has, which has no terms) lists no agent: {rows: []}, for the same inputs agentsHeader takes.
// ctx {now, replied, local} (all optional) feeds the age and the tooltip: the clock, the model's replied predicate and the per-window seen map.
// A row is one line: the program `icon` of the agent ({path, tint}, programIcon), its `name` (agentName) and `main`, its best-guess
// label when it has one, its compact `age` (null without a clock), its status shape and, in the 18 px slot at the right end, its `slot`:
// "pin" for a worker (its pin marker, with `pinTip`, "pinned" or "click to pin"), "crown" for the session's main agent (its crown, design D41 D)
// and "blank" for any other talkable agent, so the status marks stay in one column. It carries no state word and no pill (D39 D): its mark is the state, a needs-you square or a highlighted dot
// included, and `tip` holds the name, the state in words ("needs you: <kind>", "replied") and the other facts. A pin decides only the top-bar
// tab.
// -> {rows: [{id, name, icon, main, slot, shape, depth, worker, pinned, pinTip, label, parentNote, age, tip, term}]}
export function agentRows(session, pins = [], { now, replied = () => false, local = {} } = {}) {
  if (!Array.isArray(session?.terms)) return { rows: [] };   // nothing selected, or the selection is gone
  const talkable = new Set(session.talkable);
  const mainId = session.talkable[0]?.id;
  const pinned = new Set(Array.isArray(pins) ? pins : []);
  const { parent, unlisted } = sessionTree(session);
  const children = new Map();
  const top = [];
  for (const t of session.terms) {
    const p = parent.get(t.id);
    if (p === undefined) top.push(t);
    else children.set(p, [...(children.get(p) ?? []), t]);
  }
  const rows = [];
  const walk = (t, depth) => {
    const worker = !talkable.has(t);
    const state = displayState(t, { replied: rowShowsReplied(!worker, replied(t)) });
    const name = agentName(t);
    const main = mainId !== undefined && t.id === mainId;
    rows.push({
      id: t.id,
      name,
      shape: statusShape(state),
      depth,
      worker,
      pinned: pinned.has(t.id),
      pinTip: worker ? (pinned.has(t.id) ? "pinned" : "click to pin") : null,
      label: session.bestGuess && !worker ? HONESTY.bestGuess : null,
      parentNote: unlisted.has(t.id) ? "parent not listed" : null,
      icon: programIcon(t.program),
      main,
      slot: worker ? "pin" : main ? "crown" : "blank",
      age: now !== undefined && t.statusMs !== undefined ? ageShort(Math.max(0, now - t.statusMs)) : null,
      tip: `${name}: ${agFacts(t, state, worker, now, local)}`,
      term: t,
    });
    for (const c of (children.get(t.id) ?? []).sort(olderFirst)) walk(c, depth + 1);
  };
  for (const t of top.sort(olderFirst)) walk(t, unlisted.has(t.id) ? 1 : 0);
  return { rows };
}

// ---------- the pin (D37) ----------

// What a worker row's pin marker draws. Herdr's own pin (`mods/workspace-chrome/pin.svg`, drawn by its Agents list with
// `svg("mods/workspace-chrome/pin.svg").size(13)`, mods/agents/main.js:343) is an outline in both states and `svg()` takes a colour,
// not a fill, so this window ships two small drawings of its own in its side mod's folder and finds them by `mods/<mod>/<file>` as
// Herdr's tabs mod finds its own `win-*.svg` (mods/tabs/main.js:153). Pinned: an upright push-pin (head up, point down), filled, in
// the accent. Unpinned: the same pin turned 45 degrees, outline only, muted. The state reads from the shape and the colour, and
// both are one size so a pin never shifts the row. `tone` is a theme token. -> {icon, tone}
const AG_PIN_PINNED = "mods/cao-window-side/pin-filled.svg";
const AG_PIN_LOOSE = "mods/cao-window-side/pin-tilted.svg";
export const pinMark = (pinned) => (pinned === true ? { icon: AG_PIN_PINNED, tone: "accent" } : { icon: AG_PIN_LOOSE, tone: "dim" });

// ---------- the main agent's crown (D37) ----------

// The main agent of a session is the one its Sessions row speaks for, the first talkable agent (`session.talkable[0]`: the conductor or
// coordinator the user talks to). Its Agents row carries a small crown in the slot at the right end where a worker carries its pin (agentRows'
// `slot`), and its tab carries it after the name; the crown is drawn from the window's own `crown.svg`, filled, in the accent; no other agent has one, a second talkable agent and every worker included, and the Sessions row's badge has none. A session
// whose first talkable agent is only a guess (the oldest terminal) still crowns it: its "best guess" label stands beside it.
// `icon` is found by `mods/<mod>/<file>` as the pin drawings are. -> {icon, tone}
export const CROWN_MARK = Object.freeze({ icon: "mods/cao-window-side/crown.svg", tone: "accent" });

// ---------- the top bar's tabs (D37) ----------

// The tabs of a session: the agents tabsFor picks (the talkable agents and the pinned workers; one is one tab, none shown is none), each as
// {id, name, icon, main, tip, term}, in the order the Agents block lists them: agentRows' tree order (the conductor first, each agent's
// children under it in creation order, then by id), restricted to those agents. So a pinned worker comes right after its parent and
// before the parent's later sibling, and the order of the pins changes nothing. CAO owns the order of Herdr's own tabs inside a space
// (it undoes a manual drag), but this bar is a row of the window's own, so its order is the window's to set. The block lists every agent, so
// a pinned worker hangs under its real parent. The name is agentName, the one an
// Agents row shows, so the conductor and a worker are named alike and a tab carries no tag of its role. The icon is the row's. A tab
// carries no state mark (the owner found the top bar's status dot not helpful, D37 T): the state is drawn on the Agents row and the
// Sessions line, and `tip`, the row's tooltip, says it in words on the tab (a reply marks a talkable agent only). `replied(terminal)`
// is the model's predicate. No function of this module mutates its
// arguments.
export function tabViews(session, pins = [], { replied = () => false } = {}) {
  const tabs = new Set(tabsFor(session, pins).map((t) => t.id));
  if (tabs.size === 0) return [];
  return agentRows(session, pins, { replied }).rows
    .filter((r) => tabs.has(r.id))
    .map((r) => ({ id: r.id, name: r.name, icon: r.icon, main: r.main, tip: r.tip, term: r.term }));
}

// ---------- the focused terminal's row (D37) ----------

const agIsAddress = (address, f) => typeof address === "string" && (address === f || address.endsWith(`/${f}`));

// Which row of the Agents block holds the terminal Herdr has focused, so that it takes the selected fill whichever way the focus
// moved (a click on the row or on a top-bar tab, agent.next, space.select). `focus` is Herdr's `focused()`: its `terminal`, else the
// `agent` its agent block shows (a mod pane has no terminal). An address may come without its machine. `view` is what agentRows
// returned for the session. Every agent has a row, so the answer is that row's id; anything else, another session's terminal
// included, marks nothing. -> row id | null
export function focusedRow(view, focus) {
  const f = agText(focus?.terminal) ?? agText(focus?.agent);
  if (f === undefined || !Array.isArray(view?.rows)) return null;
  return view.rows.find((r) => agIsAddress(r.term?.address, f))?.id ?? null;
}

// ---------- selection and the Agents header (D25) ----------

// A stored selection -> {kind: "session", name}, or null. Anything else the shipped window stored there
// (flow, workflow and new-* kinds) or a malformed value reads as no selection, so an upgrade needs no cleanup.
export function sessionSelection(raw) {
  const ok = raw !== null && typeof raw === "object" && !Array.isArray(raw) && raw.kind === "session" && typeof raw.name === "string" && raw.name !== "";
  return ok ? { kind: "session", name: raw.name } : null;
}

// What the Agents block's header shows. `session` is the selected session from the model, or, when the
// selection names a session the list no longer has, the selection itself ({kind, name}, no terms), or null
// when nothing is selected. `flows` is the trimmed flow list ({name, schedule, enabled}); `props.
// contextPercentRegex` turns the context percentage on; `reads` is what was read for the session: the screen
// text of its talkable root once that root's turn ended ({text}). The block carries no "done means" sentence
// and no known-gap note (the status bar help has both) and no working directory (D37 R: the owner found a path
// in the Agents list not helpful).
// -> {title, note, flow: {flow, text, label, schedule} | null, recap: {text, label} | null,
//     context: {percent, label} | null}
export function agentsHeader(session, flows, props, reads) {
  const bare = { note: null, flow: null, recap: null, context: null };
  if (!session || typeof session.name !== "string") return { ...bare, title: "Select a session" };
  const title = `Agents \u00b7 ${session.name}`;
  if (!Array.isArray(session.terms)) return { ...bare, title, note: `${session.name} is not listed and may have ended.` };
  const list = Array.isArray(flows) ? flows : [];
  const started = flowOfSession(session.name, list.map((f) => f?.name));
  const row = started ? list.find((f) => f?.name === started.flow) : null;
  const text = reads?.text;
  return {
    ...bare,
    title,
    flow: started ? { ...started, schedule: row?.schedule ? (cronHuman(row.schedule) ?? row.schedule) : null } : null,
    recap: parseRecap(text),
    context: contextPercent(text, props?.contextPercentRegex),
  };
}

// ---------- the goal text, wrapped (D42 B) ----------

// The recap of the session's main agent (labelled "the agent's own words") is the one text of the left sidebar that is not cut to one
// line: it wraps up to three lines at the size of the agent rows, and an ellipsis ends the third when words are left (the full text is the
// tooltip). The toolkit has no line clamp (gpui-kit.d.ts has `truncate()` for one line and no `line_clamp` or `max_lines`), so the clamp is
// a pure function. `goalChars(width)` is the characters one line holds, from the block's width in px: 7 px a character at the 12.5 px
// size (an estimate on the safe side of the proportional font's average) after 40 px of padding and scrollbar, never under 8; a width
// that is not a finite number is the sidebar's default 268 px. `goalLines(text, width)` is the greedy word wrap of the text into that
// budget, at most GOAL_LINES lines, a word longer than a line broken at the budget, blanks folded; it gives [] for no text. When more
// than GOAL_LINES lines come out, the third is cut so that the ellipsis fits in the budget.
export const GOAL_LINES = 3;
const AG_GOAL_DEFAULT_WIDTH = 268;
export function goalChars(width) {
  const w = typeof width === "number" && Number.isFinite(width) ? width : AG_GOAL_DEFAULT_WIDTH;
  return Math.max(8, Math.floor((w - 40) / 7));
}
export function goalLines(text, width) {
  if (typeof text !== "string") return [];
  const per = goalChars(width);
  const lines = [];
  let cur = "";
  for (const word of text.split(/\s+/).filter(Boolean)) {
    if ([...word].length > per) {
      if (cur !== "") lines.push(cur);
      const chars = [...word];
      while (chars.length > per) lines.push(chars.splice(0, per).join(""));
      cur = chars.join("");
    } else if (cur === "") cur = word;
    else if ([...cur].length + 1 + [...word].length <= per) cur += ` ${word}`;
    else { lines.push(cur); cur = word; }
  }
  if (cur !== "") lines.push(cur);
  if (lines.length <= GOAL_LINES) return lines;
  const last = [...lines[GOAL_LINES - 1]];
  const kept = last.length + 1 <= per ? last : last.slice(0, per - 1);
  return [...lines.slice(0, GOAL_LINES - 1), `${kept.join("").trimEnd()}\u2026`];
}

// ---------- the Sessions header and the folded summaries (D25) ----------

const AG_FILTERS = ["asking", "replied", "working"];
const agLive = (sessions) => (Array.isArray(sessions) ? sessions : []).filter((s) => !s.draft);

// The Sessions block's header. `total` is every session, `shown` the ones the filter lists; recorded draft
// author sessions are in neither count (they stay listed with their tag whatever the filter). While a
// filter is on it reads `Sessions <M> of <S> \u00b7 filter: <name>` and has a clear control, whether or not
// anything matches; an unknown filter name lists everything, so it is no filter.
// -> {text, clearable}
export function sessionsHeader(total, shown, filter) {
  if (!AG_FILTERS.includes(filter)) return { text: "Sessions", clearable: false };
  return { text: `Sessions ${agLive(shown).length} of ${agLive(total).length} \u00b7 filter: ${filter}`, clearable: true };
}

// What a folded block shows after its name. Sessions (the sessions list): `N`, or `N, K asking` when K
// sessions have a blocked terminal; recorded drafts are not counted. Agents (the selected session's name):
// the name, or "none selected".
export function foldSummary(kind, value) {
  if (kind === "sessions") {
    const live = agLive(value);
    const asking = live.filter((s) => s.terms.some((t) => t.state === "blocked")).length;
    return asking > 0 ? `${live.length}, ${asking} asking` : `${live.length}`;
  }
  if (kind === "agents") return typeof value === "string" && value !== "" ? value : "none selected";
  return "";
}
