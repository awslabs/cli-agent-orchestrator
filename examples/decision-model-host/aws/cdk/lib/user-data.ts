import { readFileSync } from "node:fs";
import { join } from "node:path";

// Renders aws/host/user-data.sh.tmpl exactly as aws/cli/render_user_data.py does, so the stack and
// a hand-rendered host boot from the same script.

export const ROOT = join(__dirname, "..", "..", ".."); // examples/decision-model-host
const USER_DATA_LIMIT = 16 * 1024;

export interface ModelEntry {
  repo: string;
  file: string;
  sha256: string;
  name: string;
  temperature: number;
}

export interface ModelsConfig {
  llama_tag: string;
  jevk5_tag: string;
  idle_minutes: number;
  cuda_arch: number;
  models: Record<string, ModelEntry>;
}

export function loadModels(root: string = ROOT): ModelsConfig {
  return JSON.parse(readFileSync(join(root, "models.json"), "utf8")) as ModelsConfig;
}

// Python's base64.encodebytes: standard alphabet, a newline after every 76 characters.
function encodebytes(data: Buffer): string {
  const flat = data.toString("base64");
  const lines = flat.match(/.{1,76}/g) ?? [];
  return lines.map((line) => line + "\n").join("");
}

export function renderUserData(modelKey: string, root: string = ROOT): string {
  const config = loadModels(root);
  const model = config.models[modelKey];
  if (model === undefined) {
    throw new Error(`unknown model ${modelKey}; models.json has ${Object.keys(config.models).join(", ")}`);
  }
  const values: Record<string, string> = {
    MODEL_REPO: model.repo,
    MODEL_FILE: model.file,
    MODEL_SHA256: model.sha256,
    MODEL_NAME: model.name,
    MODEL_TEMP: String(model.temperature),
    LLAMA_TAG: config.llama_tag,
    JEVK5_TAG: config.jevk5_tag,
    IDLE_MINUTES: String(config.idle_minutes),
    CUDA_ARCH: String(config.cuda_arch),
    SHIM_B64: encodebytes(readFileSync(join(root, "serve_gguf.py"))),
  };
  let text = readFileSync(join(root, "aws", "host", "user-data.sh.tmpl"), "utf8");
  for (const [key, value] of Object.entries(values)) {
    text = text.split(`@${key}@`).join(value);
  }
  const left = text.match(/@[A-Z0-9_]+@/g);
  if (left) {
    throw new Error(`unfilled placeholders: ${[...new Set(left)].sort().join(", ")}`);
  }
  if (Buffer.byteLength(text) > USER_DATA_LIMIT) {
    throw new Error(`user data is ${Buffer.byteLength(text)} bytes, over EC2's 16 KiB limit`);
  }
  return text;
}
