import { useMemo, useState, useSyncExternalStore, type FormEvent } from "react";
import {
  agentCardFor,
  batch,
  groupByRunner,
  ownerText,
  propRows,
  stateText,
  isOpen,
  callText,
  isFinished,
  stateLabel,
  timeText,
  type Board,
  type Card,
  type Controller,
  type Entry,
  type Fleet,
  type FleetTerminal,
  type Panel,
  type Timeline,
  type TimelineEntry,
} from "../core/index.ts";
import type { Store } from "./store.ts";

// Everything below renders data as React text children. Nothing sets HTML
// from data, and nothing evaluates it.

export interface AppProps {
  store: Store;
  server: string;
  ambientStatus: string;
}

export function App({ store, server, ambientStatus }: AppProps) {
  const { board, panel, timeline, fleet } = useSyncExternalStore(store.subscribe, store.getSnapshot);
  const { controller } = store;
  const run = (fn: () => Promise<unknown>) => {
    void fn();
  };

  return (
    <main>
      <header>
        <h1>CAO operator dashboard</h1>
        <p>
          Server <code>{server}</code>. Ambient stream: {ambientStatus}.
        </p>
        <button type="button" onClick={() => run(() => controller.refresh())}>
          Check for approvals
        </button>
      </header>
      <Approvals board={board} controller={controller} onDone={run} />
      <FleetPanel fleet={fleet} />
      <GenUiPanel panel={panel} />
      <TimelinePanel timeline={timeline} />
    </main>
  );
}

function Approvals({
  board,
  controller,
  onDone,
}: {
  board: Board;
  controller: Controller;
  onDone: (fn: () => Promise<unknown>) => void;
}) {
  const b = useMemo(() => batch(board), [board]);
  return (
    <section aria-label="Approvals">
      <h2>Approvals (run plane)</h2>
      {board.lastError ? <p role="alert">{board.lastError}</p> : null}
      {board.needsReconcile ? (
        <p>
          The check after a failed answer did not complete, so some approvals are in an unknown state.{" "}
          <button type="button" onClick={() => onDone(() => controller.refresh())}>
            Retry the check
          </button>
        </p>
      ) : null}
      {b.waitingOn.length > 0 && b.resume.length > 0 ? (
        <p role="status">
          Nothing is sent until every open approval is decided. Still to decide: {b.waitingOn.length}.
        </p>
      ) : null}
      {b.ready ? (
        <p role="status">Every open approval is decided; the answers go out when the current run ends.</p>
      ) : null}
      {board.cards.length === 0 ? <p>No approvals listed. Use "Check for approvals".</p> : null}
      <ul>
        {board.cards.map((card) => (
          <li key={card.id}>
            <InterruptCard card={card} controller={controller} onDone={onDone} />
          </li>
        ))}
      </ul>
      <p>
        "Delivered" means the server accepted a decision for that id. The run plane does not report which
        decision was applied: another client may have answered first.
      </p>
    </section>
  );
}

function InterruptCard({
  card,
  controller,
  onDone,
}: {
  card: Card;
  controller: Controller;
  onDone: (fn: () => Promise<unknown>) => void;
}) {
  const [text, setText] = useState("");
  const open = isOpen(card);
  const submitEdit = (event: FormEvent) => {
    event.preventDefault();
    onDone(() => controller.decide(card.id, { kind: "edit", text }));
  };
  return (
    <article aria-label={`approval ${card.id}`}>
      <h3>{card.reason || "approval"}</h3>
      <dl>
        <dt>Provider</dt>
        <dd>{card.provider || "unknown"}</dd>
        <dt>Terminal</dt>
        <dd>{card.terminalId || "none"}</dd>
        <dt>Subagent</dt>
        <dd>{card.subagentRunId || "none"}</dd>
        {card.message ? (
          <>
            <dt>Message</dt>
            <dd>{card.message}</dd>
          </>
        ) : null}
        <dt>Status</dt>
        <dd data-testid="state">{stateText(card)}</dd>
        {card.hint ? (
          <>
            <dt>Ambient hint (unverified)</dt>
            <dd>{card.hint.resolved ? `resolved: ${card.hint.outcome ?? "?"}` : "open"}</dd>
          </>
        ) : null}
      </dl>
      <div>
        <button type="button" disabled={!open} onClick={() => onDone(() => controller.decide(card.id, { kind: "approve" }))}>
          Approve
        </button>
        <button type="button" disabled={!open} onClick={() => onDone(() => controller.decide(card.id, { kind: "deny" }))}>
          Deny
        </button>
        {card.state.s === "held" ? (
          <button type="button" onClick={() => onDone(async () => controller.undecide(card.id))}>
            Undo
          </button>
        ) : null}
      </div>
      {card.controls.includes("edit") ? (
        <form onSubmit={submitEdit}>
          <label>
            Edit: types this text into the agent's prompt instead of yes or no
            <input type="text" maxLength={4000} value={text} disabled={!open} onChange={(e) => setText(e.target.value)} />
          </label>
          <button type="submit" disabled={!open}>
            Send edited answer
          </button>
        </form>
      ) : null}
    </article>
  );
}

function FleetPanel({ fleet }: { fleet: Fleet }) {
  const groups = useMemo(() => groupByRunner(fleet), [fleet]);
  return (
    <section aria-label="Fleet">
      <h2>Fleet (ambient stream)</h2>
      <p>
        The agents this server can see, grouped by the runner each one runs on. A terminal with no runner is
        local. Cards come from the CAO profile catalog, not from anything an agent wrote. This panel has no
        controls.
      </p>
      {fleet.stale ? (
        <p role="status">
          The fleet may be out of sync: a change from the stream could not be applied. It is corrected by the next
          snapshot, which a reconnect brings.
        </p>
      ) : null}
      {groups.length === 0 ? <p>No terminals yet.</p> : null}
      {groups.map((group) => (
        <section key={group.runnerId === null ? "group:local" : `runner:${group.runnerId}`} aria-label={`Runner ${group.label}`}>
          <h3>{group.label}</h3>
          <p>
            {group.terminals.length} {group.terminals.length === 1 ? "terminal" : "terminals"}
            {group.waiting > 0 ? `, ${group.waiting} waiting for an answer` : ""}
          </p>
          {group.sessions.map((session) => (
            <div key={session.name}>
              <h4>
                Session {session.name || "(none)"} <small>({session.status})</small>
              </h4>
              <ul>
                {session.terminals.map((terminal) => (
                  <li key={terminal.id}>
                    <TerminalRow terminal={terminal} fleet={fleet} />
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </section>
      ))}
    </section>
  );
}

function TerminalRow({ terminal, fleet }: { terminal: FleetTerminal; fleet: Fleet }) {
  const card = agentCardFor(terminal, fleet.profiles);
  return (
    <article aria-label="agent_card">
      <h5>
        {terminal.id}: {terminal.status ?? "unknown"}
      </h5>
      <dl>
        {Object.entries(card).map(([key, value]) => (
          <div key={key}>
            <dt>{key}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
    </article>
  );
}

function GenUiPanel({ panel }: { panel: Panel }) {
  return (
    <section aria-label="Generative UI">
      <h2>Generative UI (ambient stream)</h2>
      <p>Agent-authored content, shown as text. This panel has no controls.</p>
      {panel.entries.length === 0 ? <p>No components yet.</p> : null}
      <ol>
        {panel.entries.map((entry, i) => (
          <li key={entry.eventId || `entry-${i}`}>
            <PanelEntry entry={entry} />
          </li>
        ))}
      </ol>
    </section>
  );
}

function PanelEntry({ entry }: { entry: Entry }) {
  if (entry.kind === "refused") {
    return (
      <article aria-label="refused component">
        <h3>Refused: "{entry.component}" is not on the allow-list</h3>
        <p>From {ownerText(entry.owner)}. Props are not shown.</p>
        {entry.moreRefused ? <p>And {entry.moreRefused} more refused.</p> : null}
      </article>
    );
  }
  return (
    <article aria-label={entry.component}>
      <h3>{entry.component}</h3>
      <p>From {ownerText(entry.owner)}</p>
      <dl>
        {propRows(entry.props).map(([key, value]) => (
          <div key={key}>
            <dt>{key}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
    </article>
  );
}

function TimelinePanel({ timeline }: { timeline: Timeline }) {
  return (
    <section aria-label="Session timeline">
      <h2>Session timeline (ambient stream)</h2>
      <p>
        Calls between agents, from the stream's metadata: who called whom, and whether the call is open, completed or ended.
        A call completes when the receiver's work ends, and ends as superseded when a newer call goes to the same
        receiver. After a reconnect, a call that was still open is shown as
        unconfirmed: its end may have happened while the stream was down, and the stream does not resend it. No message
        text is shown, and this panel has no controls.
      </p>
      {timeline.entries.length === 0 ? <p>No calls yet.</p> : null}
      <ol>
        {timeline.entries.map((entry) => (
          <li key={entry.toolCallId}>
            <TimelineRow entry={entry} />
          </li>
        ))}
      </ol>
    </section>
  );
}

function TimelineRow({ entry }: { entry: TimelineEntry }) {
  return (
    <article aria-label={`call ${entry.toolCallId}`}>
      <h3>{callText(entry)}</h3>
      <dl>
        <dt>State</dt>
        <dd data-testid="call-state">{stateLabel(entry)}</dd>
        <dt>Attributed to</dt>
        <dd>{ownerText(entry.owner)}</dd>
        <dt>Started</dt>
        <dd>{timeText(entry.startedAt)}</dd>
        {isFinished(entry) ? (
          <>
            <dt>{entry.state === "completed" ? "Completed" : "Ended"}</dt>
            <dd>{timeText(entry.endedAt)}</dd>
          </>
        ) : null}
      </dl>
    </article>
  );
}
