'use strict';

const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');
const {createHash} = require('node:crypto');
const {readFileSync} = require('node:fs');
const {createRequire} = require('node:module');
const path = require('node:path');
const {test} = require('node:test');

const name = 'cao-picomatch-guard';
const version = '4.0.7-cao.2';

// Run in a child with a hard deadline. Check what the compiler emits on short inputs;
// a literal fallback must not silently turn back into a repeated alternation.
if (require.main === module) {
  for (const manifest of process.argv.slice(2)) {
    const match = createRequire(manifest)('picomatch');
    for (const [pattern, ordinary, options] of [
      ['+(ab|abab)', 'ab'],
      ['*(loglog|log)', 'log'],
      ['+(Post|POSTpost)', 'Post', {nocase: true}],
      ['+("red"|redred)', 'red'],
      ['*(n"ot"e|noteno"te")', 'note'],
    ]) {
      const re = match.makeRe(pattern, options);
      assert.ok(re.test(pattern), manifest);
      assert.equal(re.test(ordinary), false, manifest);
    }
    assert.ok(match('+(read|write)')('readwrite'), manifest);
    assert.ok(match('*(notebook|note)')('notenotebook'), manifest);
    assert.ok(match('+("read"|write)')('readwrite'), manifest);
    assert.ok(match('+("read"|write)', {keepQuotes: true})('"read"write'), manifest);
    for (const [pattern, options, yes, no] of [
      ['+(*[.-0])', {windows: false}, 'a/b/', 'a/b'],
      ['+(*[a/])', {windows: false}, '[a/][a/]', 'part'],
      ['*([.-0]*)', {windows: false}, '/a/b', 'ab'],
      ['+(*[Z-^])', {windows: true}, 'a\\b\\', 'a/b/'],
      ['+(*[[:digit:]])', {windows: false}, 'part1part2', 'part1/part2'],
    ]) {
      const re = match.makeRe(pattern, options);
      assert.ok(re.test(yes), manifest);
      assert.equal(re.test(no), false, manifest);
    }
    for (const [pattern, close] of [['+(*)', ')'], ['+(*a)', ')'], ['*(*a)', ')?']]) {
      const end = match.parse(pattern).tokens.findLast(token => token.type === 'paren');
      assert.equal(end.output, close, manifest);
      assert.ok(match(pattern)('beta'), manifest);
      assert.equal(match(pattern)('beta/gamma'), false, manifest);
    }
    assert.equal(match('+(*a)')('note'), false, manifest);
  }
}

module.exports = function registerTests(project) {
  const local = createRequire(path.join(project, 'package.json'));
  const {packages} = local('./package-lock.json');
  const consumers = Object.entries(packages).filter(([location, entry]) =>
    location && [entry.dependencies, entry.optionalDependencies, entry.peerDependencies]
      .some(deps => deps?.picomatch),
  );

  test('the locked matcher is the reviewed local archive', () => {
    const archive = path.resolve(__dirname, '../vendor', `${name}-${version}.tgz`);
    const entry = packages[`node_modules/${name}`];
    assert.equal(entry.version, version);
    assert.equal(entry.license, 'MIT');
    assert.equal(entry.resolved, `file:${path.relative(project, archive).split(path.sep).join('/')}`);
    assert.equal(entry.integrity, `sha512-${createHash('sha512').update(readFileSync(archive)).digest('base64')}`);
  });

  test('every picomatch consumer loads the reviewed parser and released constants', () => {
    assert.ok(consumers.length, 'no transitive consumers to verify');
    for (const [location] of consumers) {
      const from = createRequire(path.join(project, location, 'package.json'));
      assert.equal(from('picomatch/package.json').private, true, location);
      assert.equal(from('picomatch'), local(name), location);
      const manifest = from(`${name}/package.json`);
      assert.equal(manifest.name, name, location);
      assert.equal(manifest.version, version, location);
      for (const [file, digest] of [
        ['lib/parse.js', '9ac3b9756ecaf80b24d78bdcadb4c2d92ebd4fbe50860bc904fa86aa2b7e94f1'],
        ['lib/constants.js', 'c28f952d2758261fe5428a121add485cc25b03e0193633f649de903dc0e5e222'],
      ]) {
        const bytes = readFileSync(from.resolve(`${name}/${file}`));
        assert.equal(createHash('sha256').update(bytes).digest('hex'), digest, `${location}: ${file}`);
      }
    }
  });

  test('repeated extglobs use guarded forms through each installed consumer', () => {
    const manifests = consumers.map(([location]) => path.join(project, location, 'package.json'));
    execFileSync(process.execPath, [__filename, ...manifests], {timeout: 5000});
  });

  test('unrelated glob semantics match the upstream 4.0.7 release', () => {
    const match = local('picomatch/posix');
    assert.ok(match('[!a]*')('a.js'));
    assert.equal(match('[!a]*')('b.js'), false);
    assert.ok(match('**.js')('src/a.js'));
    assert.equal(match('**/file')('a\nb/file'), false);
    assert.ok(match('[[:punct:]]')('!'));
    assert.equal(match('[[:punct:]]')('\\'), false);
  });

  if (!packages['node_modules/micromatch']) return;
  test('micromatch captures Windows path components with normalized separators', () => {
    const micromatch = local('micromatch');
    assert.deepEqual(micromatch.capture('src/**', 'src\\views\\Panel.tsx', {windows: true}),
      ['views/Panel.tsx']);
  });

  test('legacy matcher helpers default to the host without changing the POSIX entry', t => {
    const match = local('picomatch');
    t.mock.method(local('picomatch/lib/utils'), 'isWindows', () => true);
    const pattern = 'src/*.ts';
    const input = 'src\\main.ts';
    assert.ok(match(pattern)(input));
    assert.ok(match.isMatch(input, pattern));
    assert.ok(match.makeRe(pattern).test(input));
    assert.ok(match.compileRe(match.parse(pattern)).test(input));
    assert.ok(match.matchBase(input, 'main.ts'));
    assert.equal(match(pattern, {windows: false})(input), false);
    assert.equal(local('picomatch/posix')(pattern)(input), false);
  });
};
