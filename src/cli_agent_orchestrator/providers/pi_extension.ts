import { spawn } from "node:child_process";
import { mkdir, rename, unlink, writeFile } from "node:fs/promises";
import { dirname } from "node:path";
import { StringDecoder } from "node:string_decoder";

type CaoState = {
  status: "idle" | "processing" | "completed" | "error";
  lastAssistantText: string;
  error: string;
  updatedAt: string;
};

type PendingRequest = {
  resolve: (value: any) => void;
  reject: (error: Error) => void;
  cleanup: () => void;
};

type McpTool = {
  server: string;
  name: string;
  title?: string;
  description?: string;
  inputSchema: Record<string, unknown>;
};

const STATUS_KEY = "cao-pi-mcp";
const RESERVED_PI_TOOL_NAMES = new Set(["bash", "read", "edit", "write", "grep", "find", "ls"]);
const SHUTDOWN_TIMEOUT_MS = 5_000;
const SHUTDOWN_TERM_GRACE_MS = 500;
const SHUTDOWN_KILL_GRACE_MS = 500;

async function settlesWithin(promise: Promise<void>, timeoutMs: number): Promise<boolean> {
  let timeout: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      promise.then(() => true),
      new Promise<boolean>((resolve) => {
        timeout = setTimeout(() => resolve(false), timeoutMs);
      }),
    ]);
  } finally {
    if (timeout) clearTimeout(timeout);
  }
}

function requiredEnvironment(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} must be set`);
  return value;
}

function asError(error: unknown): Error {
  return error instanceof Error ? error : new Error(String(error));
}

function abortError(): Error {
  const error = new Error("MCP tool call aborted");
  error.name = "AbortError";
  return error;
}

class BridgeTerminalError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "BridgeTerminalError";
  }
}

class ProxyBridge {
  private readonly python: string;
  private readonly configPath: string;
  private readonly onTerminalFailure: (error: BridgeTerminalError) => void;
  private child: any | undefined;
  private decoder = new StringDecoder("utf8");
  private buffer = "";
  private nextRequestId = 0;
  private pending = new Map<string, PendingRequest>();
  private terminalError: BridgeTerminalError | undefined;
  private closing = false;
  private exitPromise: Promise<void> = Promise.resolve();

  constructor(
    python: string,
    configPath: string,
    onTerminalFailure: (error: BridgeTerminalError) => void,
  ) {
    this.python = python;
    this.configPath = configPath;
    this.onTerminalFailure = onTerminalFailure;
  }

  async start(): Promise<void> {
    if (this.child) return;

    this.decoder = new StringDecoder("utf8");
    this.buffer = "";
    this.terminalError = undefined;
    this.closing = false;
    const child = spawn(
      this.python,
      [
        "-m",
        "cli_agent_orchestrator.providers.pi_mcp_proxy",
        "--config",
        this.configPath,
      ],
      { env: process.env, stdio: ["pipe", "pipe", "pipe"] },
    );
    this.child = child;
    let settleExit: () => void = () => undefined;
    this.exitPromise = new Promise((resolve) => {
      let settled = false;
      settleExit = () => {
        if (settled) return;
        settled = true;
        resolve();
      };
    });
    child.stdout.on("data", (chunk: Buffer) => this.consume(chunk));
    child.stderr.on("data", (chunk: Buffer) => process.stderr.write(chunk));
    child.stdin.on("error", (error: Error) => {
      this.fail(new BridgeTerminalError(error.message), !this.closing);
    });
    child.once("error", (error: Error) => {
      if (this.child === child) this.child = undefined;
      this.fail(new BridgeTerminalError(error.message), !this.closing);
      child.stdin.destroy();
      child.stdout.destroy();
      child.stderr.destroy();
      child.unref();
      settleExit();
    });
    child.once("exit", (code: number | null, signal: string | null) => {
      if (this.child === child) this.child = undefined;
      if (!this.closing || this.pending.size > 0) {
        const reason = signal ? `signal ${signal}` : `exit code ${code}`;
        this.fail(new BridgeTerminalError(`Pi MCP proxy exited with ${reason}`), !this.closing);
      }
      settleExit();
    });

    await new Promise<void>((resolve, reject) => {
      const onSpawn = () => {
        child.off("error", onError);
        resolve();
      };
      const onError = (error: Error) => {
        child.off("spawn", onSpawn);
        reject(this.terminalError ?? new BridgeTerminalError(error.message));
      };
      child.once("spawn", onSpawn);
      child.once("error", onError);
    });
  }

  async listTools(): Promise<McpTool[]> {
    const result = await this.request("list_tools", {});
    if (!result || !Array.isArray(result.tools)) {
      throw new Error("Pi MCP proxy returned an invalid tool list");
    }
    return result.tools as McpTool[];
  }

  async callTool(
    tool: McpTool,
    arguments_: Record<string, unknown>,
    signal?: AbortSignal,
  ): Promise<any> {
    return this.request(
      "call_tool",
      { server: tool.server, name: tool.name, arguments: arguments_ },
      signal,
    );
  }

  async shutdown(): Promise<void> {
    const child = this.child;
    if (!child) return;
    this.closing = true;
    let timeout: ReturnType<typeof setTimeout> | undefined;
    const graceful = (async () => {
      await this.request("shutdown", {});
      child.stdin.end();
      await this.exitPromise;
    })();
    const outcome = await Promise.race([
      graceful.then(
        () => "complete" as const,
        () => "failed" as const,
      ),
      new Promise<"timeout">((resolve) => {
        timeout = setTimeout(() => resolve("timeout"), SHUTDOWN_TIMEOUT_MS);
      }),
    ]);
    if (timeout) clearTimeout(timeout);

    if (outcome !== "complete") {
      this.fail(new BridgeTerminalError("Pi MCP proxy did not shut down cleanly"), false);
      child.stdin.destroy();
      let exited = await settlesWithin(this.exitPromise, 0);
      if (!exited) {
        child.kill("SIGTERM");
        exited = await settlesWithin(this.exitPromise, SHUTDOWN_TERM_GRACE_MS);
      }
      if (!exited) {
        child.kill("SIGKILL");
        exited = await settlesWithin(this.exitPromise, SHUTDOWN_KILL_GRACE_MS);
      }
      if (!exited) {
        child.stdout.destroy();
        child.stderr.destroy();
        child.unref();
      }
    }
    if (this.child === child) this.child = undefined;
  }

  private request(
    type: string,
    fields: Record<string, unknown>,
    signal?: AbortSignal,
  ): Promise<any> {
    if (signal?.aborted) return Promise.reject(abortError());
    if (!this.child || this.terminalError) {
      return Promise.reject(this.terminalError ?? new Error("Pi MCP proxy is not running"));
    }

    const child = this.child;
    const id = String(++this.nextRequestId);
    return new Promise((resolve, reject) => {
      const onAbort = () => {
        this.pending.delete(id);
        cleanup();
        void this.request("cancel", { targetId: id }).catch(() => undefined);
        reject(abortError());
      };
      const cleanup = () => signal?.removeEventListener("abort", onAbort);
      this.pending.set(id, { resolve, reject, cleanup });
      signal?.addEventListener("abort", onAbort, { once: true });

      child.stdin.write(`${JSON.stringify({ id, type, ...fields })}\n`, (error?: Error) => {
        if (!error) return;
        this.fail(new BridgeTerminalError(error.message), !this.closing);
      });
    });
  }

  private consume(chunk: Buffer): void {
    this.buffer += this.decoder.write(chunk);
    let newline = this.buffer.indexOf("\n");
    while (newline !== -1) {
      let line = this.buffer.slice(0, newline);
      this.buffer = this.buffer.slice(newline + 1);
      if (line.endsWith("\r")) line = line.slice(0, -1);
      if (line) this.consumeLine(line);
      newline = this.buffer.indexOf("\n");
    }
  }

  private consumeLine(line: string): void {
    let response: any;
    try {
      response = JSON.parse(line);
    } catch {
      this.fail(new BridgeTerminalError("Pi MCP proxy returned malformed JSON"));
      return;
    }
    if (!response || typeof response.id !== "string" || typeof response.ok !== "boolean") {
      this.fail(new BridgeTerminalError("Pi MCP proxy returned an invalid response"));
      return;
    }

    const pending = this.pending.get(response.id);
    if (!pending) return;
    this.pending.delete(response.id);
    pending.cleanup();
    if (response.ok) pending.resolve(response.result);
    else pending.reject(new Error(String(response.error || "Pi MCP proxy request failed")));
  }

  private fail(error: BridgeTerminalError, publish = true): void {
    if (!this.terminalError) this.terminalError = error;
    for (const pending of this.pending.values()) {
      pending.cleanup();
      pending.reject(this.terminalError);
    }
    this.pending.clear();
    if (publish && this.terminalError === error) this.onTerminalFailure(error);
  }
}

function assistantText(message: any): string | undefined {
  if (!message || message.role !== "assistant" || !Array.isArray(message.content)) {
    return undefined;
  }
  return message.content
    .filter((item: any) => item?.type === "text" && typeof item.text === "string")
    .map((item: any) => item.text)
    .join("");
}

function unsupportedContent(item: any): string {
  if (item?.type === "resource_link" && typeof item.uri === "string") {
    return `${item.title || item.name || "MCP resource"}: ${item.uri}`;
  }
  if (item?.type === "audio") {
    return `[MCP audio content${item.mimeType ? ` (${item.mimeType})` : ""}]`;
  }
  try {
    return JSON.stringify(item);
  } catch {
    return String(item);
  }
}

function normalizeMcpContent(result: any): Array<Record<string, string>> {
  const normalized: Array<Record<string, string>> = [];
  for (const item of Array.isArray(result?.content) ? result.content : []) {
    if (item?.type === "text" && typeof item.text === "string") {
      normalized.push({ type: "text", text: item.text });
    } else if (
      item?.type === "image" &&
      typeof item.data === "string" &&
      typeof item.mimeType === "string"
    ) {
      normalized.push({ type: "image", data: item.data, mimeType: item.mimeType });
    } else if (item?.type === "resource" && item.resource) {
      if (typeof item.resource.text === "string") {
        normalized.push({ type: "text", text: item.resource.text });
      } else if (
        typeof item.resource.blob === "string" &&
        typeof item.resource.mimeType === "string" &&
        item.resource.mimeType.startsWith("image/")
      ) {
        normalized.push({
          type: "image",
          data: item.resource.blob,
          mimeType: item.resource.mimeType,
        });
      } else {
        normalized.push({ type: "text", text: unsupportedContent(item) });
      }
    } else {
      normalized.push({ type: "text", text: unsupportedContent(item) });
    }
  }
  if (normalized.length === 0 && result?.structuredContent !== undefined) {
    normalized.push({ type: "text", text: JSON.stringify(result.structuredContent) });
  }
  if (normalized.length === 0) normalized.push({ type: "text", text: "" });
  return normalized;
}

export default function caoPiExtension(pi: any) {
  const stateFile = requiredEnvironment("CAO_PI_STATE_FILE");
  let activeContext: any | undefined;
  let terminalFailure: BridgeTerminalError | undefined;
  let terminalReport: Promise<void> | undefined;
  const bridge = new ProxyBridge(
    requiredEnvironment("CAO_PI_BRIDGE_PYTHON"),
    requiredEnvironment("CAO_PI_MCP_CONFIG"),
    (error) => {
      terminalFailure = error;
      if (!activeContext) return;
      void reportBridgeFailure(activeContext, error).catch((stateError) => {
        activeContext.ui.setStatus(
          STATUS_KEY,
          `MCP error: ${error.message}; state update failed: ${asError(stateError).message}`,
        );
      });
    },
  );
  let currentState: CaoState = {
    status: "idle",
    lastAssistantText: "",
    error: "",
    updatedAt: "",
  };
  let cachedAssistantText = "";
  let writeSequence = 0;
  let writeQueue: Promise<void> = Promise.resolve();
  let bridgeFailed = false;
  const registeredToolNames = new Set<string>();

  function writeState(patch: Partial<Omit<CaoState, "updatedAt">>): Promise<void> {
    currentState = { ...currentState, ...patch, updatedAt: new Date().toISOString() };
    const snapshot = JSON.stringify(currentState);
    const temporary = `${stateFile}.${process.pid}.${++writeSequence}.tmp`;
    writeQueue = writeQueue.then(async () => {
      await mkdir(dirname(stateFile), { recursive: true, mode: 0o700 });
      try {
        await writeFile(temporary, snapshot, { encoding: "utf8", mode: 0o600 });
        await rename(temporary, stateFile);
      } catch (error) {
        await unlink(temporary).catch(() => undefined);
        throw error;
      }
    });
    return writeQueue;
  }

  async function reportBridgeFailure(ctx: any, error: unknown): Promise<void> {
    const normalized = asError(error);
    if (terminalFailure === normalized && terminalReport) return terminalReport;
    bridgeFailed = true;
    const message = normalized.message;
    ctx.ui.setStatus(STATUS_KEY, `MCP error: ${message}`);
    const report = writeState({ status: "error", error: message });
    if (terminalFailure === normalized) terminalReport = report;
    await report;
  }

  function toPiTool(tool: McpTool): any {
    if (
      !tool ||
      typeof tool.server !== "string" ||
      typeof tool.name !== "string" ||
      !tool.name ||
      !tool.inputSchema ||
      typeof tool.inputSchema !== "object" ||
      Array.isArray(tool.inputSchema)
    ) {
      throw new Error("Pi MCP proxy returned an invalid tool definition");
    }
    return {
      name: tool.name,
      label: typeof tool.title === "string" && tool.title ? tool.title : tool.name,
      description: typeof tool.description === "string" ? tool.description : "MCP tool",
      parameters: tool.inputSchema,
      async execute(
        _toolCallId: string,
        params: Record<string, unknown>,
        signal: AbortSignal | undefined,
        _onUpdate: unknown,
        ctx: any,
      ) {
        try {
          const result = await bridge.callTool(tool, params, signal);
          const content = normalizeMcpContent(result);
          if (result?.isError) {
            throw new Error(
              content
                .filter((item) => item.type === "text")
                .map((item) => item.text)
                .join("\n") || "MCP tool failed",
            );
          }
          return { content, details: { server: tool.server, result } };
        } catch (error) {
          if (error instanceof BridgeTerminalError) {
            await reportBridgeFailure(ctx, error);
          }
          throw error;
        }
      },
    };
  }

  pi.on("session_start", async (_event: any, ctx: any) => {
    activeContext = ctx;
    try {
      await bridge.start();
      const tools = await bridge.listTools();
      for (const tool of tools) {
        if (RESERVED_PI_TOOL_NAMES.has(tool?.name)) {
          throw new Error(`Pi MCP proxy returned reserved Pi tool name: ${tool.name}`);
        }
      }
      for (const tool of tools) {
        if (registeredToolNames.has(tool.name)) continue;
        pi.registerTool(toPiTool(tool));
        registeredToolNames.add(tool.name);
      }
      if (terminalFailure) {
        await reportBridgeFailure(ctx, terminalFailure);
        throw terminalFailure;
      }
      ctx.ui.setStatus(STATUS_KEY, undefined);
      await writeState({ status: "idle", lastAssistantText: "", error: "" });
    } catch (error) {
      await reportBridgeFailure(ctx, error);
      throw error;
    }
  });

  pi.on("agent_start", async () => {
    if (bridgeFailed) return;
    await writeState({ status: "processing", error: "" });
  });

  pi.on("message_end", async (event: any) => {
    if (bridgeFailed) return;
    const text = assistantText(event.message);
    if (text !== undefined) cachedAssistantText = text;
  });

  pi.on("agent_settled", async () => {
    if (bridgeFailed) return;
    await writeState({
      status: "completed",
      lastAssistantText: cachedAssistantText,
      error: "",
    });
  });

  pi.on("session_shutdown", async (_event: any, ctx: any) => {
    try {
      await bridge.shutdown();
    } catch (error) {
      await reportBridgeFailure(ctx, error);
    }
  });
}
