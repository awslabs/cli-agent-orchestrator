// Entry point for ui://cao/graph. Creates the MCP App bridge and mounts the
// GraphView. No localStorage / sessionStorage / cookies are used anywhere.

import React from "react";
import { createRoot } from "react-dom/client";
import "../shared/styles.css";
import { McpApp } from "../shared/mcpApp";
import { GraphView } from "./GraphView";

const app = new McpApp();
const container = document.getElementById("root")!;
createRoot(container).render(
  <React.StrictMode>
    <GraphView
      app={app}
      onOpenTopic={(nodeId) => {
        void app.silentlyNoteToModel(`Opened graph node ${nodeId}`);
      }}
    />
  </React.StrictMode>,
);
