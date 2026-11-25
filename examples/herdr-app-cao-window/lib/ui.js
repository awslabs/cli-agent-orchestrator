// Helpers the mods share: the status mark, focusing a terminal and opening the dialog. Unlike the other
// shared modules this one imports the host modules, so it cannot run under node and has no node test;
// the mods are checked with `herdr-app check`.
//
// The imports carry ui-prefixed names. Bundle mode puts every shared module and the mod's own code
// in one scope, where a second binding of the mod's own `div` or `run` would be a syntax error.
import { div as uiDiv, image as uiImage, svg as uiSvg } from "gpui-kit";
import { h_flex as uiRow } from "gpui-base";
import { agents as uiAgents, run as uiRun, openOverlay as uiOpenOverlay, props as uiProps, shared as uiShared, setShared as uiSetShared } from "herdr";
import { randomUUID as uiUuid } from "crypto";
import { dialogLive, dialogSpec, leaseFor } from "./shell.js";

// agent.focus for a terminal Herdr lists as an agent, pane.focus for one it does not.
export function focusTerminal(address) {
  if (uiAgents().some((a) => a.address === address)) uiRun("agent.focus", { terminal: address });
  else uiRun("pane.focus", { terminal: address });
}

// The pin marker of a worker row: one of the window's own two pin drawings (see pinMark in lib/agents.js), 14 px in the theme token
// the mark names. `mark` is pinMark's {icon, tone}; `c` is the theme. One size whatever the state, so the row does not shift.
export function pinIcon(mark, c) {
  return uiSvg(mark.icon).size(14).flex_none().text_color(c[mark.tone]);
}

// The logo of an agent's program, as Herdr's lists draw it (mods/agents/main.js:80): a one-colour glyph through svg() in `color` when
// the program says it is tinted, else a picture through image(), at `size` px. `icon` is programIcon's {path, tint}; the path is a
// file in the home folder, so no icon is installed.
export function programMark(icon, c, size, color) {
  return icon.tint ? uiSvg(icon.path).size(size).flex_none().text_color(color) : uiImage(icon.path).w(size).h(size).flex_none();
}

// A session's letter badge, as Herdr's spaces list draws a space's avatar (mods/spaces/main.js:502): 18 px, rounded 5, the first letter
// in 10.5 px bold white. `badge` is sessionLine's {letter, color}; `fill` is badgeFill's hex for its colour name.
export function sessionBadge(badge, fill) {
  return uiRow().size(18).flex_none().rounded(5).bg(fill).items_center().justify_center().text_size(10.5).font_bold().text_color("#ffffff").child(badge.letter);
}

// The crown of a session's main agent: the window's own filled drawing (see CROWN_MARK in lib/agents.js), 14 px in the theme token the
// mark names, the size of the pin and of the state mark's widest slot. `mark` is CROWN_MARK's {icon, tone}; `c` is the theme.
export function crownIcon(mark, c) {
  return uiSvg(mark.icon).size(14).flex_none().text_color(c[mark.tone]);
}

// One status mark per mark name of statusShape (lib/logic.js), drawn as Herdr's own Agents list draws it (the shipped
// mods/agents/main.js `status()`, lines 212-219): the same spinner file, sizes, spacing and theme tokens, so a mark here is the
// mark there. `c` is the theme. The mark sits in a 14 px slot (the widest of them) so that the names after it line up. `spinner-dim` is
// the working spinner in the muted token, for an agent that waits on its workers.
export function shapeMark(name, c) {
  const marks = {
    spinner: () => uiSvg("mods/workspace-chrome/spinner.svg").size(12).mx(1).flex_none().text_color(c.working).spin(900),
    "spinner-dim": () => uiSvg("mods/workspace-chrome/spinner.svg").size(12).mx(1).flex_none().text_color(c.dim).spin(900),
    bang: () => uiDiv().size(14).flex_none().rounded(4).bg(c.blocked + "26").text_size(10.5).font_bold().text_color(c.blocked).flex().items_center().justify_center().child("!"),
    ring: () => uiDiv().size(7).mx(3).flex_none().rounded(4).border(1.5).border_color(c.idle ?? c.dim),
    dot: () => uiDiv().size(7).mx(3).flex_none().rounded(4).bg(c.done ?? c.dim),
    "dot-hi": () => uiDiv().size(7).mx(3).flex_none().rounded(4).bg(c.ansi2 ?? c.done ?? c.dim),
  };
  return uiRow().w(14).flex_none().items_center().justify_center().child(marks[name]?.() ?? uiDiv().size(7).mx(3).flex_none());
}

// The dialog's lease key and the mod whose overlay hosts every form.
const UI_DIALOG_KEY = "caowin.dialog";
const UI_PANEL_MOD = "cao-window-panel";

// Opens the dialog for `request` ({kind, ...}), unless one is open: the lease `caowin.dialog` is read against this
// clock and refuses only while it is live. Otherwise it writes the lease for a new name (`dlg-<8 hex>`, passed to
// the dialog in its request) and opens the overlay in the same tick, with the opener's own layout props merged in,
// because an overlay copy gets only what it is passed. Returns whether a dialog was opened.
export function openDialog(request) {
  const now = Date.now();
  if (dialogLive(uiShared(UI_DIALOG_KEY), now)) return false;
  const name = `dlg-${uiUuid().replace(/-/g, "").slice(0, 8)}`;
  uiSetShared(UI_DIALOG_KEY, leaseFor(name, now));
  uiOpenOverlay({ mod: UI_PANEL_MOD, ...dialogSpec({ ...request, name }, uiProps()) });
  return true;
}
