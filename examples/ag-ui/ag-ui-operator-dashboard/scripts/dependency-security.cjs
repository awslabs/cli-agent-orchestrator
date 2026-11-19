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
require('../../../../scripts/test-picomatch-security.cjs')(root);
const manifest = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
const lock = JSON.parse(fs.readFileSync(path.join(root, 'package-lock.json'), 'utf8'));
const packages = Object.entries(lock.packages).filter(([location]) => location);

// Packages allowed to declare an install script. npm ci runs with
// ignore-scripts (.npmrc), so even these never run one.
const INSTALL_SCRIPT_ALLOWED = new Map([
  ['node_modules/fsevents', 'optional macOS file watcher for Vite; ships a prebuilt binary'],
]);

const EXACT = /^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$/;

// The shared matcher check verifies this archive's path, version, license and SHA-512.
const matcherName = 'cao-picomatch-guard';
const matcherArchive = 'file:../../../vendor/cao-picomatch-guard-4.0.7-cao.3.tgz';
const directSpecs = Object.values({...manifest.dependencies, ...manifest.devDependencies});
const localDirs = directSpecs.filter(spec => spec.startsWith('file:') && spec !== matcherArchive);
const localPackages = new Map(localDirs.map(spec => {
  const dir = spec.slice('file:'.length);
  assert.equal(path.basename(dir), dir, 'local packages must be direct project directories');
  assert.equal(path.dirname(fs.realpathSync(path.join(root, dir))), root);
  const pkg = JSON.parse(fs.readFileSync(path.join(root, dir, 'package.json'), 'utf8'));
  assert.equal(pkg.private, true);
  assert.match(pkg.version, EXACT);
  return [dir, pkg];
}));

test('every external direct dependency is pinned to an exact version', () => {
  for (const pkg of [manifest, ...localPackages.values()]) {
    for (const field of ['dependencies', 'devDependencies', 'optionalDependencies', 'peerDependencies']) {
      for (const [name, spec] of Object.entries(pkg[field] ?? {})) {
        if (name === matcherName && spec === matcherArchive) continue;
        if (spec.startsWith('file:') && localPackages.has(spec.slice(5))) continue;
        assert.match(spec, EXACT, `${pkg.name}: ${field}.${name} is "${spec}", not an exact version`);
      }
    }
  }
});

test('the app and its local packages declare no install lifecycle scripts', () => {
  for (const pkg of [manifest, ...localPackages.values(), require(`${matcherName}/package.json`)]) {
    for (const hook of ['preinstall', 'install', 'postinstall', 'prepare', 'prepublish']) {
      assert.equal(pkg.scripts?.[hook], undefined, `${pkg.name} declares a ${hook} script`);
    }
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

test('packages have registry integrity, a verified archive, or a private project link', () => {
  for (const [dir, pkg] of localPackages) {
    assert.equal(lock.packages[dir].name ?? path.basename(dir), pkg.name);
    assert.equal(lock.packages[dir].version, pkg.version);
  }
  for (const [location, entry] of packages) {
    if (entry.link) {
      assert.ok(localPackages.has(entry.resolved), `${location} is not a project package link`);
      continue;
    }
    if (localPackages.has(location)) continue;
    if (location === `node_modules/${matcherName}`) {
      assert.equal(entry.resolved, matcherArchive);
    } else {
      assert.ok(entry.resolved?.startsWith('https://registry.npmjs.org/'),
        `${location} resolves to ${entry.resolved}`);
    }
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
