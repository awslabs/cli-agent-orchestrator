// Component tests for gesture -> primitive mapping and the silent
// model-context note (Phase III).
//
// Covers:
//   - each TaskControl gesture maps to exactly one SubmitCommandKind,
//   - the gesture payload uses the field the server route reads
//     (terminal_id / session_name),
//   - destructive gestures require window.confirm() before submitting,
//   - drag-and-drop reassignment maps to the `assign` kind,
//   - model-context notes are silent (no inference trigger), body-free, and
//     failure-tolerant.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import {
  buildGesturePayload,
  describeGesture,
  DRAG_REASSIGN_KIND,
} from "../shared/mcpApp";
import { TaskControl } from "../shared/TaskControl";
import type { SubmitCommandKind } from "../shared/types";

const ALL_KINDS: SubmitCommandKind[] = [
  "send_message",
  "assign",
  "interrupt",
  "pause",
  "resume",
  "shutdown_session",
];

type SubmitFn = (
  kind: SubmitCommandKind,
  payload: Record<string, unknown>,
) => Promise<{ success: boolean; error?: string }>;

function renderControl(
  opts: {
    confirm?: (message: string) => boolean;
    target?: string;
    scopes?: string[];
  } = {},
) {
  const impl: SubmitFn = async () => ({ success: true });
  const onSubmit = vi.fn(impl);
  const confirm = vi.fn(opts.confirm ?? ((_m: string) => true));
  render(
    <TaskControl
      onSubmit={onSubmit}
      target={opts.target ?? "term-1"}
      scopes={opts.scopes}
      confirm={confirm}
    />,
  );
  return { onSubmit, confirm };
}

describe("buildGesturePayload", () => {
  it("maps shutdown_session target to session_name", () => {
    expect(buildGesturePayload("shutdown_session", "cao-x")).toEqual({
      session_name: "cao-x",
    });
  });

  it("maps terminal-scoped gestures to terminal_id", () => {
    for (const kind of [
      "send_message",
      "assign",
      "interrupt",
      "pause",
      "resume",
    ] as SubmitCommandKind[]) {
      expect(buildGesturePayload(kind, "term-1", { message: "x" })).toEqual({
        terminal_id: "term-1",
        message: "x",
      });
    }
  });

  it("create_session carries only caller extras (no target field)", () => {
    expect(
      buildGesturePayload("create_session", "ignored", {
        agent_profile: "dev",
      }),
    ).toEqual({ agent_profile: "dev" });
  });
});

describe("describeGesture", () => {
  it("returns a body-free Semantic_Primitive description for every kind", () => {
    for (const kind of ALL_KINDS) {
      const note = describeGesture(kind, "term-1");
      expect(note).toContain("term-1");
      // body-free: never includes a raw message body marker
      expect(note.length).toBeLessThan(120);
    }
    expect(describeGesture("shutdown_session", "cao-x")).toContain(
      "completion",
    );
    expect(describeGesture("assign", "term-1")).toContain("handoff");
  });
});

describe("TaskControl gesture mapping", () => {
  it("each button submits exactly one kind with the right payload field", async () => {
    // Each gesture is tested on a fresh render: a click sets `busy`, which
    // disables the other buttons until the submit resolves.
    for (const kind of ["send_message", "assign", "interrupt"] as const) {
      const { onSubmit } = renderControl();
      if (kind === "send_message" || kind === "assign") {
        fireEvent.change(screen.getByTestId("task-input"), {
          target: { value: "do the thing" },
        });
      }
      fireEvent.click(screen.getByTestId(`btn-${kind}`));
      await Promise.resolve();
      expect(onSubmit).toHaveBeenCalledOnce();
      expect(onSubmit.mock.calls[0][0]).toBe(kind);
      expect(onSubmit.mock.calls[0][1]).toHaveProperty("terminal_id", "term-1");
      cleanup();
    }
  });

  it("requires confirm() before a destructive shutdown", async () => {
    const denied = renderControl({ confirm: vi.fn(() => false) });
    fireEvent.click(screen.getByTestId("btn-shutdown_session"));
    await Promise.resolve();
    expect(denied.confirm).toHaveBeenCalledOnce();
    expect(denied.onSubmit).not.toHaveBeenCalled();
  });

  it("submits the destructive shutdown once confirmed (session_name payload)", async () => {
    const ok = renderControl({ target: "cao-x", confirm: vi.fn(() => true) });
    fireEvent.click(screen.getByTestId("btn-shutdown_session"));
    await Promise.resolve();
    expect(ok.onSubmit).toHaveBeenCalledWith("shutdown_session", {
      session_name: "cao-x",
    });
  });

  it("maps a drag-and-drop reassignment to the assign kind", async () => {
    const { onSubmit } = renderControl();
    const zone = screen.getByTestId("task-control");
    fireEvent.drop(zone, {
      dataTransfer: { getData: () => "reassigned task text" },
    });
    await Promise.resolve();
    expect(onSubmit).toHaveBeenCalledOnce();
    expect(onSubmit.mock.calls[0][0]).toBe(DRAG_REASSIGN_KIND);
    expect(onSubmit.mock.calls[0][0]).toBe("assign");
    expect(onSubmit.mock.calls[0][1]).toMatchObject({
      terminal_id: "term-1",
      message: "reassigned task text",
    });
  });
});

describe("silent model-context note", () => {
  it("silentlyNoteToModel posts exactly one update and is failure-tolerant", async () => {
    const { McpApp } = await import("../shared/mcpApp");
    const app = new McpApp();
    const update = vi
      .spyOn(app, "updateModelContext")
      .mockResolvedValue(undefined);

    await app.silentlyNoteToModel(describeGesture("assign", "term-1"));
    expect(update).toHaveBeenCalledOnce();
    // body-free, token-efficient text content (not structuredContent)
    const arg = update.mock.calls[0][0];
    expect(arg).toEqual([
      { type: "text", text: describeGesture("assign", "term-1") },
    ]);
  });

  it("swallows update failures without throwing (never blocks the iframe)", async () => {
    const { McpApp } = await import("../shared/mcpApp");
    const app = new McpApp();
    vi.spyOn(app, "updateModelContext").mockRejectedValue(new Error("boom"));
    await expect(app.silentlyNoteToModel("note")).resolves.toBeUndefined();
  });
});
