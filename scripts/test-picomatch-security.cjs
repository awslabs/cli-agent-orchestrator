'use strict';

const assert = require('node:assert/strict');
const {createHash} = require('node:crypto');
const {readFileSync} = require('node:fs');
const {createRequire} = require('node:module');
const path = require('node:path');
const {test} = require('node:test');

module.exports = function registerTests(project) {
  const local = createRequire(path.join(project, 'package.json'));
  const {packages} = local('./package-lock.json');
  const consumers = Object.entries(packages).filter(([location, entry]) =>
    location && [entry.dependencies, entry.optionalDependencies, entry.peerDependencies]
      .some(deps => deps?.picomatch),
  );

  test('every picomatch consumer loads the reviewed parser', () => {
    assert.ok(consumers.length, 'no transitive consumers to verify');
    for (const [location] of consumers) {
      const from = createRequire(path.join(project, location, 'package.json'));
      assert.equal(from('picomatch/package.json').private, true, location);
      assert.equal(from('picomatch'), local('@ryotsu/picomatch'), location);
      const manifest = from('@ryotsu/picomatch/package.json');
      assert.equal(manifest.name, '@ryotsu/picomatch', location);
      assert.equal(manifest.version, '4.0.7-a', location);
      const parser = readFileSync(from.resolve('@ryotsu/picomatch/lib/parse.js'));
      assert.equal(createHash('sha256').update(parser).digest('hex'),
        'a59b25c67cce014f5226836f8391cb1f323dc76224dc82b85c6826d10b33922a', location);
    }
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
