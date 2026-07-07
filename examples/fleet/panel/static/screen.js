import { parseAnsiLine } from "./ansi.js";
import { computeFollow } from "./follow.js";

// ANSI colour index (0-15) → CSS var. Defaults to --text when null.
function fgVar(fg) {
  return fg == null ? "var(--text)" : `var(--ansi-${fg})`;
}

// Render a full screen frame (string) into `el`.
//
// Container height is governed entirely by CSS — tiles are a fixed height with
// overflow:hidden, the console is flex:1 + min-height:0 + overflow:auto and
// scrolls internally. We deliberately do NOT force a min-height on the frame:
// doing so grew the console to the full frame height and pushed the input bar
// off-screen. `opts.follow` is "always" (pin bottom, tiles) or "smart" (only
// follow when the user is already at the bottom, console).
export function renderScreen(el, frame, opts) {
  const follow = computeFollow(opts.follow, el);
  const prevTop = el.scrollTop;
  const frag = document.createDocumentFragment();
  for (const rawLine of (frame || "").split("\n")) {
    const lineEl = document.createElement("div");
    lineEl.className = "screen-line";
    for (const seg of parseAnsiLine(rawLine)) {
      const span = document.createElement("span");
      span.textContent = seg.text;
      span.style.color = fgVar(seg.fg);
      if (seg.bold) span.style.fontWeight = "600";
      lineEl.appendChild(span);
    }
    frag.appendChild(lineEl);
  }
  el.replaceChildren(frag);
  if (follow) el.scrollTop = el.scrollHeight;
  else el.scrollTop = prevTop; // preserve the user's manual scroll position
}
