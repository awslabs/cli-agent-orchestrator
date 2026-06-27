// Entry point for ui://cao/event-stream.

import React from "react";
import { createRoot } from "react-dom/client";
import "../shared/styles.css";
import { McpApp } from "../shared/mcpApp";
import { EventStreamView } from "./EventStreamView";

const app = new McpApp();
const container = document.getElementById("root")!;
createRoot(container).render(
  <React.StrictMode>
    <EventStreamView app={app} />
  </React.StrictMode>,
);
