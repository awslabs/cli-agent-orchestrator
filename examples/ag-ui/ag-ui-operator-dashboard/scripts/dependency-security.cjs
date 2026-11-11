'use strict';

// Dependency checks for this example app, run by `npm run test:dependencies`
// (node --test). They mirror web/'s test:dependencies gate in purpose: the
// installed graph is the one the lockfile pins, and nothing in it runs code at
// install time that we have not looked at.
//
// web/ needs the guarded `braces` fork because patch-package pulls micromatch
// into its graph. This app has no patch-package and no postinstall, so its
// graph must not contain `braces` at all. If it ever does, the same shared
// check web/ runs (scripts/test-braces-security.cjs) applies here too.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {test} = require('node:test');

const root = path.resolve(__dirname, '..');
const manifest = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
const lock = JSON.parse(fs.readFileSync(path.join(root, 'package-lock.json'), 'utf8'));
const packages = Object.entries(lock.packages).filter(([location]) => location);

// Packages allowed to declare an install script. npm ci runs with
// ignore-scripts (.npmrc), so even these never run one.
const INSTALL_SCRIPT_ALLOWED = new Map([
  ['node_modules/fsevents', 'optional macOS file watcher for Vite; ships a prebuilt binary'],
]);

const EXACT = /^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$/;

test('every direct dependency is pinned to an exact version', () => {
  for (const field of ['dependencies', 'devDependencies', 'optionalDependencies']) {
    for (const [name, spec] of Object.entries(manifest[field] ?? {})) {
      assert.match(spec, EXACT, `${field}.${name} is "${spec}", not an exact version`);
    }
  }
});

test('the app declares no install lifecycle scripts', () => {
  for (const hook of ['preinstall', 'install', 'postinstall', 'prepare', 'prepublish']) {
    assert.equal(manifest.scripts?.[hook], undefined, `package.json declares a ${hook} script`);
  }
});

test('npm is configured to skip install scripts', () => {
  const npmrc = fs.readFileSync(path.join(root, '.npmrc'), 'utf8');
  assert.match(npmrc, /^ignore-scripts=true$/m);
});

test('the lockfile matches package.json', () => {
  const locked = lock.packages[''];
  for (const field of ['dependencies', 'devDependencies']) {
    assert.deepEqual(locked[field] ?? {}, manifest[field] ?? {}, `${field} differ from the lockfile`);
  }
});

test('every locked package comes from the npm registry with an integrity hash', () => {
  for (const [location, entry] of packages) {
    if (entry.link) continue;
    assert.ok(
      entry.resolved?.startsWith('https://registry.npmjs.org/'),
      `${location} resolves to ${entry.resolved}`,
    );
    assert.match(entry.integrity ?? '', /^sha512-/, `${location} has no sha512 integrity`);
  }
});

test('only reviewed packages declare an install script', () => {
  for (const [location, entry] of packages) {
    if (entry.hasInstallScript) {
      assert.ok(INSTALL_SCRIPT_ALLOWED.has(location), `${location} declares an install script`);
    }
  }
});

const bracesConsumers = packages.filter(
  ([, entry]) => entry.dependencies?.braces || entry.optionalDependencies?.braces,
);

if (bracesConsumers.length > 0) {
  require('../../../../scripts/test-braces-security.cjs')(root);
} else {
  test('the graph contains no braces consumer (the guarded fork is not needed)', () => {
    assert.equal(lock.packages['node_modules/braces'], undefined);
  });
}
