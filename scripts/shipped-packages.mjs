// Records which npm packages a build actually bundled, for the dependency gate (#1398).
//
// `scripts/dependency_security.py` blocks a pull request only on findings in code CAO
// ships. The shipped set is measured from the build output (the modules the bundler
// pulled in), not from the `dependencies` / `devDependencies` labels in package.json,
// which are wrong in both directions.
//
// Output, one file per build config, never inside a shipped output directory:
//   <project>/.shipped/bundled-packages.<config>.json
//   {"schema": 1, "project": "web", "config": "web",
//    "output_dir": "src/cli_agent_orchestrator/web_ui",   // repo-relative, or null
//    "outputs": ["assets/index-abc.js", ...],             // names relative to output_dir
//    "packages": [{"name": "react", "version": "19.3.1"}]}
//
// What counts as bundled (#1400): every node_modules module in an output chunk, the
// packages of `build.rollupOptions.external` (shipped as runtime imports, never in the
// module graph), and the packages that own virtual helper modules the bundler injects
// (`\0vite/modulepreload-polyfill`, `\0rolldown/runtime`). An unknown virtual module
// throws: code the plugin cannot attribute must not be silently dropped.
//
// A build that bundles no package at all throws: an empty set is treated as a failed
// producer by the gate, so it is better to fail here, next to the cause.

import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";

const NODE_MODULES = "/node_modules/";

function toPosix(path) {
  return path.replaceAll("\\", "/");
}

/** Virtual-module prefixes the bundler itself injects, and the package that owns each. */
const VIRTUAL_OWNERS = [
  ["\0vite/", "vite"],
  ["\0rolldown/", "rolldown"],
  ["\0rollup/", "rollup"],
  ["\0commonjsHelpers", "@rollup/plugin-commonjs"],
];

/** Strip a bundler query (`?v=1`); virtual modules (`\0` prefix) return null. */
function cleanId(id) {
  if (typeof id !== "string" || id.startsWith("\0")) return null;
  return toPosix(id.split("?")[0]);
}

/** Owning package of a virtual module id, or throw: unattributed injected code fails closed. */
export function virtualOwner(id) {
  for (const [prefix, name] of VIRTUAL_OWNERS) {
    if (id.startsWith(prefix)) return name;
  }
  throw new Error(`shipped-packages: cannot attribute virtual module ${JSON.stringify(id)}`);
}

/** `{dir, name}` of the package that owns a module path, or null if it is not in node_modules. */
export function owningPackage(moduleId) {
  const id = cleanId(moduleId);
  if (!id) return null;
  const at = id.lastIndexOf(NODE_MODULES);
  if (at < 0) return null;
  const parts = id.slice(at + NODE_MODULES.length).split("/");
  const nameParts = parts[0].startsWith("@") ? parts.slice(0, 2) : parts.slice(0, 1);
  if (nameParts.length === 0 || nameParts.some((part) => !part)) return null;
  const name = nameParts.join("/");
  return { dir: id.slice(0, at + NODE_MODULES.length) + name, name };
}

function readManifest(dir) {
  const manifest = join(dir, "package.json");
  if (!existsSync(manifest)) return null;
  const { name, version } = JSON.parse(readFileSync(manifest, "utf8"));
  if (typeof version !== "string" || !version) return null;
  return { name: typeof name === "string" && name ? name : null, version };
}

function readVersion(dir) {
  return readManifest(dir)?.version ?? null;
}

/**
 * Record a package under its folder name AND, for an npm alias (`"x": "npm:@scope/y@1"`),
 * under the real name from its own package.json, which is the name Trivy reports.
 */
function addPackage(found, folderName, dir, version) {
  found.set(`${folderName}@${version}`, { name: folderName, version });
  const real = dir ? readManifest(dir)?.name : null;
  if (real && real !== folderName) found.set(`${real}@${version}`, { name: real, version });
}

/** `file:` dependencies are symlinks that bundlers resolve to the real directory. */
function linkedPackages(projectRoot) {
  const lock = join(projectRoot, "package-lock.json");
  if (!existsSync(lock)) return [];
  const entries = JSON.parse(readFileSync(lock, "utf8")).packages ?? {};
  const linked = [];
  for (const [key, entry] of Object.entries(entries)) {
    if (!entry || entry.link !== true || typeof entry.resolved !== "string") continue;
    const at = key.lastIndexOf("node_modules/");
    if (at < 0) continue;
    const dir = toPosix(resolve(projectRoot, entry.resolved));
    const version = readVersion(dir);
    if (version) linked.push({ dir, name: key.slice(at + "node_modules/".length), version });
  }
  return linked;
}

function installedVersion(projectRoot, name) {
  const version = readVersion(join(projectRoot, "node_modules", name));
  if (!version) {
    throw new Error(`shipped-packages: ${name} is referenced by the build but not installed`);
  }
  return version;
}

/** Packages named by `build.rollupOptions.external` (strings only; anything else throws). */
export function externalPackages(external, projectRoot) {
  if (external === undefined || external === null) return [];
  const list = Array.isArray(external) ? external : [external];
  const found = [];
  for (const item of list) {
    if (typeof item !== "string") {
      throw new Error(
        "shipped-packages: build.rollupOptions.external must list plain package names; a regular expression or function cannot be enumerated",
      );
    }
    if (item.startsWith("node:") || item.startsWith(".") || item.startsWith("/")) continue;
    const parts = item.split("/");
    const name = item.startsWith("@") ? parts.slice(0, 2).join("/") : parts[0];
    found.push({ name, version: installedVersion(projectRoot, name) });
  }
  return found;
}

/** Reduce bundler module ids to sorted, unique `{name, version}` pairs. */
export function collectPackages(moduleIds, projectRoot, extra = []) {
  const linked = linkedPackages(projectRoot);
  const found = new Map();
  for (const pkg of extra) addPackage(found, pkg.name, join(projectRoot, "node_modules", pkg.name), pkg.version);
  for (const raw of moduleIds) {
    if (typeof raw === "string" && raw.startsWith("\0")) {
      const name = virtualOwner(raw);
      const version = installedVersion(projectRoot, name);
      addPackage(found, name, join(projectRoot, "node_modules", name), version);
      continue;
    }
    const id = cleanId(raw);
    if (!id) continue;
    const link = linked.find((item) => id === item.dir || id.startsWith(`${item.dir}/`));
    if (link) {
      addPackage(found, link.name, link.dir, link.version);
      continue;
    }
    const owner = owningPackage(id);
    if (!owner) continue;
    const version = readVersion(owner.dir);
    if (!version) {
      throw new Error(`shipped-packages: no readable version for ${owner.dir}`);
    }
    addPackage(found, owner.name, owner.dir, version);
  }
  return [...found.values()].sort(
    (a, b) => a.name.localeCompare(b.name) || a.version.localeCompare(b.version),
  );
}

export function writeBundledPackages({
  projectRoot,
  project,
  config,
  packages,
  outputDir = null,
  outputs = [],
}) {
  if (packages.length === 0) {
    throw new Error(
      `shipped-packages: ${project} (${config}) bundled no npm package; refusing to record an empty shipped set`,
    );
  }
  const directory = join(projectRoot, ".shipped");
  mkdirSync(directory, { recursive: true });
  const file = join(directory, `bundled-packages.${config}.json`);
  writeFileSync(
    file,
    `${JSON.stringify({ schema: 1, project, config, output_dir: outputDir, outputs, packages }, null, 2)}\n`,
    "utf8",
  );
  return file;
}

/**
 * Vite/Rollup plugin. `projectRoot` defaults to the resolved Vite root; pass it when the
 * config sets a different `root` (the MCP App views build from `src/<view>`). The record is
 * written in `writeBundle`, after every other plugin (including the single-file inliner) has
 * finished, so `outputs` lists exactly the files that were emitted. Register it in
 * `worker.plugins` too: worker bundles are built in a separate pass.
 */
export function bundledPackagesPlugin({ project, config, projectRoot } = {}) {
  let root = projectRoot;
  let outDir = null;
  let external;
  let packages = [];
  return {
    name: "cao-bundled-packages",
    apply: "build",
    configResolved(resolved) {
      root = resolve(root ?? resolved.root);
      const repoRoot = resolve(root, "..");
      const out = resolve(resolved.root, resolved.build.outDir);
      outDir = out.startsWith(`${repoRoot}/`) ? toPosix(out.slice(repoRoot.length + 1)) : null;
      external = (resolved.build.rolldownOptions ?? resolved.build.rollupOptions ?? {}).external;
    },
    generateBundle(_options, bundle) {
      const ids = [];
      for (const output of Object.values(bundle)) {
        if (output.type === "chunk") ids.push(...Object.keys(output.modules ?? {}));
      }
      packages = collectPackages(ids, root, externalPackages(external, root));
    },
    writeBundle(_options, bundle) {
      writeBundledPackages({
        projectRoot: root,
        project,
        config,
        packages,
        outputDir: outDir,
        outputs: Object.keys(bundle).sort(),
      });
    },
  };
}

/**
 * Docusaurus plugin: records the packages of BOTH webpack compilations. `client` is the code
 * that reaches readers' browsers; `server` is the static-render pass that writes the HTML
 * of every page, so a package in it can write shipped output.
 */
export function docusaurusClientPackagesPlugin(context) {
  const projectRoot = resolve(context.siteDir);
  return {
    name: "cao-client-bundle-packages",
    configureWebpack(_config, isServer) {
      const config = isServer ? "server" : "client";
      return {
        plugins: [
          {
            apply(compiler) {
              compiler.hooks.done.tap("cao-client-bundle-packages", (stats) => {
                const ids = [];
                const visit = (module) => {
                  if (module.resource) ids.push(module.resource);
                  for (const inner of module.modules ?? []) visit(inner);
                };
                for (const module of stats.compilation.modules) visit(module);
                writeBundledPackages({
                  projectRoot,
                  project: "docusaurus",
                  config,
                  packages: collectPackages(ids, projectRoot),
                });
              });
            },
          },
        ],
      };
    },
  };
}
