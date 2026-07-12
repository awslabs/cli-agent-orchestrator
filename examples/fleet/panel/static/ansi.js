// Minimal ANSI-SGR parser for capture-pane -e output.
// capture-pane renders the final cell grid, so only SGR (colour/bold) escapes
// remain — cursor-movement CSI sequences, if any, carry no text and are dropped.

const CSI = /\x1b\[([0-9;]*)([A-Za-z])/g;

export function parseAnsiLine(line) {
  const segs = [];
  let fg = null;
  let bold = false;
  let last = 0;
  const flush = (text) => {
    if (!text) return;
    const prev = segs[segs.length - 1];
    if (prev && prev.fg === fg && prev.bold === bold) prev.text += text;
    else segs.push({ text, fg, bold });
  };
  CSI.lastIndex = 0;
  let m;
  while ((m = CSI.exec(line))) {
    flush(line.slice(last, m.index));
    last = CSI.lastIndex;
    if (m[2] === "m") {
      const codes = m[1] === "" ? [0] : m[1].split(";").map((n) => parseInt(n, 10));
      for (const code of codes) {
        if (code === 0) { fg = null; bold = false; }
        else if (code === 1) bold = true;
        else if (code === 22) bold = false;
        else if (code >= 30 && code <= 37) fg = code - 30;
        else if (code >= 90 && code <= 97) fg = code - 90 + 8;
        else if (code === 39) fg = null;
      }
    }
    // non-'m' CSI (e.g. \x1b[2K): consumed, emits no text
  }
  flush(line.slice(last));
  if (segs.length === 0) return [{ text: "", fg: null, bold: false }];
  return segs;
}
