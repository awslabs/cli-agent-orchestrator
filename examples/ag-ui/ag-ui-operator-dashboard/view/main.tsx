import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { Root } from "./Root.tsx";
import { chooseToken } from "./token.ts";

// The server comes from ?server=, default the local cao-server. A token, when
// auth is on, is read from sessionStorage (key "caoToken") and kept in memory.
// It is sent as an Authorization header, never in a URL, and only to a server
// on this machine unless the operator confirms (view/token.ts).
const choice = chooseToken(window.location.search, window.sessionStorage, (m) => window.confirm(m));
const threadId = `operator-dashboard-${crypto.randomUUID()}`;

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Root choice={choice} threadId={threadId} />
  </StrictMode>,
);
