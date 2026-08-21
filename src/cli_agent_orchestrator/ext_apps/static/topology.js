// Phase 1 / commit 7: minimal SSE-driven topology event log.
// Subscribes to /events on the same origin and renders each event as a
// list item. Future commits replace this with a real fleet visualisation.
(function () {
  "use strict";

  const status = document.getElementById("status");
  const log = document.getElementById("event-log");
  const MAX_EVENTS = 200;

  function setStatus(state, text) {
    status.className = "status status-" + state;
    status.textContent = text;
  }

  function renderEvent(event) {
    const li = document.createElement("li");
    const type = document.createElement("span");
    type.className = "event-type";
    // /events publishes normalized rows shaped as
    //   { id, kind, terminal_id, session_name, timestamp, detail }
    // (see services/event_log_service.py). Render the semantic `kind` as the
    // type label and `detail` as the payload. The earlier `type`/`payload`
    // names never existed on the wire, so every row showed "?" / "{}".
    type.textContent = event.kind || "?";
    const payload = document.createElement("span");
    payload.className = "event-payload";
    // Surface the available routing context (session / terminal) so a row is
    // meaningful instead of a bare detail blob.
    const ctx = [event.session_name, event.terminal_id]
      .filter(Boolean)
      .join(" / ");
    payload.textContent =
      (ctx ? ctx + " " : "") + JSON.stringify(event.detail || {});
    li.appendChild(type);
    li.appendChild(payload);
    log.insertBefore(li, log.firstChild);
    while (log.childElementCount > MAX_EVENTS) {
      log.removeChild(log.lastChild);
    }
  }

  // Allow the host to override the events URL (Claude.ai sandbox sets a
  // different origin for MCP App content). Defaults to same-origin /events.
  const EVENTS_URL = window.CAO_EVENTS_URL || "/events";

  let source;
  try {
    source = new EventSource(EVENTS_URL);
  } catch (e) {
    setStatus("error", "EventSource unavailable");
    return;
  }

  source.addEventListener("open", function () {
    setStatus("connected", "connected");
  });

  source.addEventListener("error", function () {
    setStatus("error", "error");
  });

  source.addEventListener("message", function (msg) {
    try {
      const event = JSON.parse(msg.data);
      renderEvent(event);
    } catch (e) {
      // Ignore malformed events; the producer is supposed to send JSON.
    }
  });
})();
