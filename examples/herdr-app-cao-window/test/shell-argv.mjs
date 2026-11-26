// Test helper: what a POSIX shell makes of a command line that the window offers to copy. The command
// name is replaced by a function that prints each argument followed by NUL, so the test sees the exact
// argv the shell would pass. A command that quotes badly shows as a different argv (an injected
// `$(echo ...)` is run by the shell and its output lands inside an argument).
import { spawnSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { mkTmp } from "./harness.mjs";

// Returns the arguments after `command`. `command` is the first word of the line, for example "cao".
// The shell runs in `cwd` (default: the folder the test runs in), so a test that wants an unquoted `*`
// to show up as a glob gives it a folder that holds files.
export function shellArgv(line, command, { cwd } = {}) {
  const script = `${command}() { for a in "$@"; do printf '%s\\0' "$a"; done; }\n${line}\n`;
  const r = spawnSync("sh", ["-c", script], { encoding: "utf8", cwd });
  if (r.status !== 0) throw new Error(`sh exited ${r.status}: ${r.stderr}`);
  const parts = r.stdout.split("\0");
  parts.pop();
  return parts;
}

// Single words with no space that a shell reads as something else when they are not quoted. Some are
// special as a whole word (expansions, separators, a comment, `=cmd`), some only after the first
// character (a glob character in the middle of a name, as in the developer-role tool `fs_*`), and some
// only as the first character (a separator or a redirection in front of a name).
export const HARD_WORDS = Object.freeze([
  // the whole word
  "$HOME", "$(id)", "a;b", "`id`", "*", "?", "~", "a&b", "x|y", "a>b", "a<b", "(x)", "{a,b}", "\"q\"", "'q'", "\\", "#", "!", "=ls",
  // a glob character after the first character; globDir() holds files each of these matches
  "fs_*", "a*", "x?", "a*b", "a?b", "a[b]",
  // another metacharacter after the first character
  "a~b", "a!b", "a#b", "a(b", "a{b", "a}b", "a[b",
  // a separator, a redirection or a glob bracket as the first character
  ";x", "|x", "&x", ">x", "<x", "^x", "[x", "~root",
]);

// True when a word has to be quoted to be safe in a shell: any character outside letters, digits and
// _ . / : @ % + , = -, or a leading = (zsh expands =cmd).
export const needsQuotes = (word) => /[^A-Za-z0-9_./:@%+,=-]/.test(word) || word.startsWith("=");

// How many times a word that needs quotes appears in the line as one quoted token. A word that holds a
// single quote is quoted by splitting it, so its text never appears whole and is not counted here.
export function quotedCount(line, word) {
  return line.split(`'${word}'`).length - 1;
}

// A scratch folder that holds a few files, to run the shell in: an unquoted glob then expands to them.
// fs_read matches fs_*, x1 matches x?, axb matches a?b and a*b, ab matches a[b].
export function globDir() {
  const dir = mkTmp("cao-window-glob-");
  for (const name of ["a", "b", "x", "q", "id", "fs_read", "x1", "axb", "ab"]) writeFileSync(join(dir, name), "");
  return dir;
}
