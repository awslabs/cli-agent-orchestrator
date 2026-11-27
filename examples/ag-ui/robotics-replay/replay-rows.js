// ABOUTME: Pure row builder for the robotics replay page: recorded AG-UI records plus a factory command log in, timeline rows out.
// ABOUTME: No DOM, no network, no clock. The page and the node test both load this file, so they cannot disagree.
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.RoboticsReplayRows = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // Row kinds, in the order the legend shows them.
  var KINDS = ["agent", "call", "approval", "factory"];

  function meta(frame) {
    return (frame.metadata && frame.metadata.cao) || {};
  }

  // records: parsed lines of a cao-dojo-trace/1 file without the _meta line.
  // commands: the "commands" array of the factory command log.
  // Returns [{kind, label, detail, seq, after}] in replay order. A factory command
  // follows the TOOL_CALL_START whose ordinal equals its after_tool_calls; ordinal 0
  // comes first. Factory commands are not AG-UI events, so they carry no seq.
  function buildRows(records, commands) {
    var rows = [];
    var opened = {}; // toolCallId -> row index of its TOOL_CALL_START
    var calls = 0;
    var byAfter = {};
    (commands || []).forEach(function (c) {
      (byAfter[c.after_tool_calls] = byAfter[c.after_tool_calls] || []).push(c);
    });
    function flushFactory(after) {
      (byAfter[after] || []).forEach(function (c) {
        rows.push({
          kind: "factory",
          label: c.actor_zone + ": " + c.operation + "(" + c.arguments.join(", ") + ")",
          detail: c.status + (c.reason ? " (" + c.reason + ")" : "") + ", sim " + c.sim_seconds + " s",
          seq: null,
          after: after,
        });
      });
      delete byAfter[after];
    }
    flushFactory(0);
    records.forEach(function (r) {
      var f = r.frame;
      if (r.plane === "run" && r.dir === "request") {
        var resume = (r.request && r.request.resume) || [];
        resume.forEach(function (entry) {
          rows.push({
            kind: "approval",
            label: "operator resumes the run",
            detail: "approved: " + String(entry.payload && entry.payload.approved),
            seq: r.seq,
          });
        });
        return;
      }
      if (r.plane !== "stream" || !f) return;
      var cao = meta(f);
      if (f.type === "SUBAGENT_STARTED") {
        rows.push({
          kind: "agent",
          label: cao.terminalId ? "agent starts: " + cao.terminalId : "session starts",
          detail: cao.terminalId ? f.name + " (" + (cao.provider || "") + ")" : f.name,
          seq: r.seq,
        });
      } else if (f.type === "SUBAGENT_FINISHED") {
        rows.push({
          kind: "agent",
          label: cao.terminalId ? "agent ends: " + cao.terminalId : "session ends",
          detail: "endReason: " + (cao.endReason || ""),
          seq: r.seq,
        });
      } else if (f.type === "TOOL_CALL_START") {
        calls += 1;
        opened[f.toolCallId] = rows.length;
        rows.push({
          kind: "call",
          label: cao.sender + " -> " + cao.receiver + ": " + f.toolCallName,
          detail: "opened",
          seq: r.seq,
          ordinal: calls,
        });
        flushFactory(calls);
      } else if (f.type === "TOOL_CALL_END") {
        var start = rows[opened[f.toolCallId]];
        rows.push({
          kind: "call",
          label: start ? start.label : f.toolCallId,
          detail: "closed by " + (cao.closedBy || "unknown"),
          seq: r.seq,
        });
      } else if (f.type === "CUSTOM" && f.name === "cao.generative_ui" && f.value) {
        var v = f.value;
        if (v.component !== "approval_card") return;
        var p = v.props || {};
        rows.push({
          kind: "approval",
          label: "approval card" + (p.resolved ? " resolved" : " raised"),
          detail: p.resolved ? "outcome: " + p.outcome : "options: " + (p.options || []).join(", "),
          seq: r.seq,
        });
      }
    });
    // Any command whose ordinal never appeared (a shorter trace) goes last, in order.
    Object.keys(byAfter)
      .map(Number)
      .sort(function (a, b) { return a - b; })
      .forEach(flushFactory);
    return rows;
  }

  function parseNdjson(text) {
    var lines = text.split("\n").filter(function (l) { return l.trim() !== ""; });
    var parsed = lines.map(function (l) { return JSON.parse(l); });
    return { meta: parsed[0]._meta, records: parsed.slice(1) };
  }

  return { KINDS: KINDS, buildRows: buildRows, parseNdjson: parseNdjson };
});
