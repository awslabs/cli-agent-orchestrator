import { describe, expect, it } from "vitest";
import { controlsFor, EDIT_MAX_CHARS, isBlankForServer, payloadFor } from "../controls.ts";
import { interruptsOf } from "./helpers.ts";

const approveDeny = interruptsOf("one-interrupt.sse")[0].responseSchema;
const editOffered = interruptsOf("edit-offered.generated.sse")[0].responseSchema;

describe("controlsFor", () => {
  it("offers edit only when responseSchema carries editedArgs", () => {
    expect(controlsFor(approveDeny)).toEqual(["approve", "deny"]);
    expect(controlsFor(editOffered)).toEqual(["approve", "deny", "edit"]);
    expect(controlsFor(undefined)).toEqual(["approve", "deny"]);
    expect(controlsFor({ type: "object" })).toEqual(["approve", "deny"]);
  });
});

describe("payloadFor", () => {
  it("approve and deny carry status: resolved and a boolean", () => {
    expect(payloadFor("i", { kind: "approve" }, approveDeny)).toEqual({
      ok: true,
      entry: { interruptId: "i", status: "resolved", payload: { approved: true } },
    });
    expect(payloadFor("i", { kind: "deny" }, approveDeny)).toEqual({
      ok: true,
      entry: { interruptId: "i", status: "resolved", payload: { approved: false } },
    });
  });

  it("edit always sends approved: true with the text", () => {
    expect(payloadFor("i", { kind: "edit", text: "write to the build dir" }, editOffered)).toEqual({
      ok: true,
      entry: { interruptId: "i", status: "resolved", payload: { approved: true, editedArgs: "write to the build dir" } },
    });
  });

  // Python's str.strip() also removes U+001C..U+001F and U+0085, which JS's \s does not.
  it.each(["", "  ", "\t", " \u00a0 ", "\x1c", "\x1d\x1e\x1f", "\x85", " \x85\u3000\u2028 "])(
    "refuses blank edit text %j: nothing to send",
    (text) => {
      expect(isBlankForServer(text)).toBe(true);
      expect(payloadFor("i", { kind: "edit", text }, editOffered)).toEqual({
        ok: false,
        reason: "edited text is empty; nothing was sent",
      });
    },
  );

  it("every code point Python's str.isspace() accepts is blank here too", () => {
    // [chr(c) for c in range(0x110000) if chr(c).isspace()], Python 3.13
    const python = [
      0x9, 0xa, 0xb, 0xc, 0xd, 0x1c, 0x1d, 0x1e, 0x1f, 0x20, 0x85, 0xa0, 0x1680, 0x2000, 0x2001, 0x2002, 0x2003,
      0x2004, 0x2005, 0x2006, 0x2007, 0x2008, 0x2009, 0x200a, 0x2028, 0x2029, 0x202f, 0x205f, 0x3000,
    ];
    for (const cp of python) expect(isBlankForServer(String.fromCodePoint(cp)), cp.toString(16)).toBe(true);
  });

  it("text with any visible character is not blank", () => {
    expect(isBlankForServer(" x ")).toBe(false);
    expect(isBlankForServer("\x1cy\x85")).toBe(false);
    expect(payloadFor("i", { kind: "edit", text: "\x85ok" }, editOffered)).toMatchObject({ ok: true });
  });

  it("refuses edit where the prompt does not offer it, even with text", () => {
    expect(payloadFor("i", { kind: "edit", text: "x" }, approveDeny)).toEqual({
      ok: false,
      reason: "this prompt does not accept edited text",
    });
  });

  it("refuses multi-line or over-long text instead of letting the server cut it", () => {
    expect(payloadFor("i", { kind: "edit", text: "a\nb" }, editOffered)).toMatchObject({ ok: false });
    expect(payloadFor("i", { kind: "edit", text: "a\rb" }, editOffered)).toMatchObject({ ok: false });
    expect(payloadFor("i", { kind: "edit", text: "x".repeat(EDIT_MAX_CHARS + 1) }, editOffered)).toMatchObject({
      ok: false,
      reason: `edited text is longer than ${EDIT_MAX_CHARS} characters`,
    });
    expect(payloadFor("i", { kind: "edit", text: "x".repeat(EDIT_MAX_CHARS) }, editOffered)).toMatchObject({ ok: true });
  });
});
