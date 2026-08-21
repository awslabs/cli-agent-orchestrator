// Entry point for ui://cao/dashboard. Creates the MCP App bridge and mounts the
// Dashboard. No localStorage / sessionStorage / cookies are used anywhere.

import React from "react";
import { createRoot } from "react-dom/client";
import "../shared/styles.css";
import { McpApp } from "../shared/mcpApp";
import { Dashboard } from "./Dashboard";

const app = new McpApp();
const container = document.getElementById("root")!;
createRoot(container).render(
  <React.StrictMode>
    <Dashboard app={app} />
  </React.StrictMode>,
);
