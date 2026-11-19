// Node tests for scripts/shipped-packages.mjs (#1398, #1400 item E). Run by web's
// `npm run test:dependencies` (node --test), the same runner the braces checks use, and
// so by the Dependency Security job's mitigation step.
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  bundledPackagesPlugin,
  collectPackages,
  externalPackages,
  owningPackage,
  virtualOwner,
} from "./shipped-packages.mjs";

const REPO = fileURLToPath(new URL("..", import.meta.url));

function project(packages) {
  const root = mkdtempSync(join(tmpdir(), "cao-shipped-"));
  for (const [name, version] of Object.entries(packages)) {
    const dir = join(root, "node_modules", name);
    mkdirSync(dir, { recursive: true });
    writeFileSync(join(dir, "package.json"), JSON.stringify({ name, version }));
  }
  return root;
}

test("owningPackage handles scoped and nested packages", () => {
  assert.deepEqual(owningPackage("/x/node_modules/@scope/pkg/index.js").name, "@scope/pkg");
  assert.equal(owningPackage("/x/node_modules/a/node_modules/b/i.js").name, "b");
  assert.equal(owningPackage("/x/src/app.ts"), null);
});

test("injected virtual helpers are attributed to the bundler that owns them", () => {
  assert.equal(virtualOwner("\0vite/modulepreload-polyfill.js"), "vite");
  assert.equal(virtualOwner("\0rolldown/runtime.js"), "rolldown");
});

test("an unknown virtual module fails instead of being dropped", () => {
  assert.throws(() => virtualOwner("\0some-plugin/helper"), /cannot attribute/);
});

test("collectPackages records virtual helpers and externals as shipped", () => {
  const root = project({ vite: "7.0.0", react: "19.0.0", lodash: "4.17.21" });
  const packages = collectPackages(
    [`${root}/node_modules/react/index.js`, "\0vite/modulepreload-polyfill.js"],
    root,
    externalPackages(["lodash", "node:fs", "./local"], root),
  );
  assert.deepEqual(
    packages.map((p) => `${p.name}@${p.version}`),
    ["lodash@4.17.21", "react@19.0.0", "vite@7.0.0"],
  );
});

test("an external that cannot be enumerated fails", () => {
  const root = project({});
  assert.throws(() => externalPackages([/^lodash/], root), /plain package names/);
  assert.throws(() => externalPackages(["not-installed"], root), /not installed/);
});

test("the plugin writes packages and the emitted file names", () => {
  const repo = mkdtempSync(join(tmpdir(), "cao-repo-"));
  const root = join(repo, "web");
  mkdirSync(join(root, "node_modules", "react"), { recursive: true });
  writeFileSync(join(root, "node_modules", "react", "package.json"), '{"version":"19.0.0"}');
  const plugin = bundledPackagesPlugin({ project: "web", config: "web", projectRoot: root });
  plugin.configResolved({ root, build: { outDir: "../dist/ui", rollupOptions: {} } });
  const bundle = {
    "assets/a.js": { type: "chunk", modules: { [`${root}/node_modules/react/index.js`]: {} } },
    "assets/a.css": { type: "asset" },
  };
  plugin.generateBundle({}, bundle);
  plugin.writeBundle({}, bundle);
  const record = JSON.parse(
    readFileSync(join(root, ".shipped", "bundled-packages.web.json"), "utf8"),
  );
  assert.equal(record.output_dir, "dist/ui");
  assert.deepEqual(record.outputs, ["assets/a.css", "assets/a.js"]);
  assert.deepEqual(record.packages, [{ name: "react", version: "19.0.0" }]);
});

test("the plugin refuses to record an empty bundle", () => {
  const root = project({});
  const plugin = bundledPackagesPlugin({ project: "web", config: "web", projectRoot: root });
  plugin.configResolved({ root, build: { outDir: "out" } });
  plugin.generateBundle({}, {});
  assert.throws(() => plugin.writeBundle({}, {}), /bundled no npm package/);
});

test("both Vite configs register the plugin for worker bundles too", () => {
  for (const config of ["web/vite.config.ts", "cao_mcp_apps/vite.config.ts"]) {
    const text = readFileSync(join(REPO, config), "utf8");
    assert.match(text, /worker:\s*{[\s\S]*?bundledPackagesPlugin\(/, config);
  }
});

test("an npm alias is recorded under its folder name AND its real package name", () => {
  // docusaurus installs @slorber/react-helmet-async in node_modules/react-helmet-async;
  // Trivy reports the real name, so the record must carry it too.
  const root = mkdtempSync(join(tmpdir(), "cao-alias-"));
  const dir = join(root, "node_modules", "react-helmet-async");
  mkdirSync(dir, { recursive: true });
  writeFileSync(
    join(dir, "package.json"),
    JSON.stringify({ name: "@slorber/react-helmet-async", version: "1.3.0" }),
  );
  const packages = collectPackages([`${dir}/lib/index.js`], root);
  assert.deepEqual(
    packages.map((p) => p.name),
    ["@slorber/react-helmet-async", "react-helmet-async"],
  );
  assert.ok(packages.every((p) => p.version === "1.3.0"));
});
