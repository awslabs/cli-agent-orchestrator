// Entry point for ui://cao/agent.

import React from "react";
import { createRoot } from "react-dom/client";
import "../shared/styles.css";
import { McpApp } from "../shared/mcpApp";
import { AgentView } from "./AgentView";

const app = new McpApp();
const container = document.getElementById("root")!;
createRoot(container).render(
  <React.StrictMode>
    <AgentView app={app} />
  </React.StrictMode>,
);
