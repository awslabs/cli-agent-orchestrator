'use strict';

const assert = require('node:assert/strict');
const {createRequire} = require('node:module');
const path = require('node:path');
const {test} = require('node:test');
const {runInNewContext} = require('node:vm');

const maxDepth = 100;
const methods = ['parse', 'compile', 'expand', 'stringify'];
const forms = [
  ['braces', '{', '}'],
  ['parentheses', '(', ')'],
];

function bounded(run) {
  return runInNewContext('run()', {run}, {timeout: 1000});
}

function nestedAst(depth, type = 'root') {
  const root = {type, nodes: []};
  let parent = root;
  for (let i = type === 'root' ? 0 : 1; i < depth; i++) {
    const child = {type: 'paren', nodes: [], parent};
    parent.nodes.push(child);
    parent = child;
  }
  parent.nodes.push({type: 'text', value: 'x', parent});
  return root;
}

function registerTests(project) {
  const localRequire = createRequire(path.join(project, 'package.json'));
  const braces = localRequire('braces');
  const micromatch = localRequire('micromatch');
  const lock = localRequire('./package-lock.json');

  for (const method of methods) {
    for (const [name, open, close] of forms) {
      test(`${method} bounds ${name} nesting before exhausting the stack`, () => {
        assert.doesNotThrow(() =>
          bounded(() =>
            braces[method](
              open.repeat(maxDepth) + 'x' + close.repeat(maxDepth),
            ),
          ),
        );
        for (const depth of [maxDepth + 1, 3500, 4000]) {
          const pattern = open.repeat(depth) + 'x' + close.repeat(depth);
          assert.throws(() => bounded(() => braces[method](pattern)), {
            name: 'SyntaxError',
            message: /exceeds max depth/,
          });
        }
      });
    }

    test(`${method} honors a lower limit without allowing the safety cap to increase`, () => {
      assert.doesNotThrow(() =>
        bounded(() => braces[method]('{a,b}', {maxDepth: 1.5})),
      );
      assert.throws(
        () => bounded(() => braces[method]('{{a,b},c}', {maxDepth: 1.5})),
        {
          name: 'SyntaxError',
          message: /exceeds max depth/,
        },
      );
      const pattern = '{'.repeat(maxDepth + 1) + 'x' + '}'.repeat(maxDepth + 1);
      for (const limit of [1000, Infinity, NaN]) {
        assert.throws(
          () => bounded(() => braces[method](pattern, {maxDepth: limit})),
          {
            name: 'SyntaxError',
            message: /exceeds max depth/,
          },
        );
      }
    });
  }

  for (const method of ['compile', 'expand', 'stringify']) {
    test(`${method} also bounds caller-supplied ASTs`, () => {
      assert.doesNotThrow(() =>
        bounded(() => braces[method](nestedAst(maxDepth))),
      );
      assert.doesNotThrow(() =>
        bounded(() => braces[method](nestedAst(maxDepth, 'paren'))),
      );
      assert.throws(
        () => bounded(() => braces[method](nestedAst(maxDepth + 1, 'paren'))),
        {
          name: 'RangeError',
          message: /exceeds max depth/,
        },
      );
      assert.throws(
        () => bounded(() => braces[method](nestedAst(2), {maxDepth: 1.5})),
        {
          name: 'RangeError',
          message: /exceeds max depth/,
        },
      );
      for (const limit of [undefined, 1000, Infinity, NaN]) {
        assert.throws(
          () =>
            bounded(() =>
              braces[method](nestedAst(maxDepth + 1), {maxDepth: limit}),
            ),
          {
            name: 'RangeError',
            message: /exceeds max depth/,
          },
        );
      }
      const cycle = {type: 'root', nodes: []};
      cycle.nodes.push(cycle);
      assert.throws(() => bounded(() => braces[method](cycle)), {
        name: 'RangeError',
        message: /exceeds max depth/,
      });
    });
  }

  test('expand bounds cyclic AST parent links', () => {
    for (const length of [1, 2]) {
      const parents = Array.from({length}, () => ({
        type: 'paren',
        nodes: [],
      }));
      for (let i = 0; i < length; i++) {
        parents[i].parent = parents[(i + 1) % length];
      }
      parents[0].nodes.push({type: 'text', value: 'x'});
      for (const ast of [parents[0], {type: 'root', nodes: [parents[0]]}]) {
        assert.throws(() => bounded(() => braces.expand(ast)), {
          name: 'RangeError',
          message: /AST parent depth .* exceeds max depth/,
        });
      }
    }
  });

  test('expand bounds acyclic AST parent chains', () => {
    function expandWithParents(depth, options) {
      const ast = {
        type: 'paren',
        nodes: [{type: 'text', value: 'x'}],
      };
      let parent = ast;
      for (let i = 1; i < depth; i++) {
        parent.parent = {type: 'paren'};
        parent = parent.parent;
      }
      parent.parent = {type: 'root', queue: []};
      return bounded(() => braces.expand(ast, options));
    }

    assert.deepEqual(expandWithParents(maxDepth), ['x']);
    assert.deepEqual(expandWithParents(1, {maxDepth: 1.5}), ['x']);
    assert.throws(() => expandWithParents(2, {maxDepth: 1.5}), {
      name: 'RangeError',
      message: /AST parent depth .* exceeds max depth/,
    });
    for (const limit of [undefined, 1000, Infinity, NaN]) {
      assert.throws(() => expandWithParents(maxDepth + 1, {maxDepth: limit}), {
        name: 'RangeError',
        message: /AST parent depth .* exceeds max depth/,
      });
    }
  });

  test('ordinary nested sets, ranges, escapes, and glob matching retain their behavior', () => {
    assert.equal(braces.compile('lib/{api,cli}'), 'lib/(api|cli)');
    assert.deepEqual(braces.expand('file-{01..03}.{js,ts}'), [
      'file-01.js',
      'file-01.ts',
      'file-02.js',
      'file-02.ts',
      'file-03.js',
      'file-03.ts',
    ]);
    assert.deepEqual(braces.expand('src/({api,cli})'), [
      'src/(api)',
      'src/(cli)',
    ]);
    assert.deepEqual(braces.expand(String.raw`file-\{literal\}-{a,b}`), [
      'file-{literal}-a',
      'file-{literal}-b',
    ]);
    for (const quote of ['"', "'"]) {
      const quoted = quote + String.raw`dir\\` + quote + '/{a,b}';
      assert.deepEqual(braces.expand(quoted), [
        String.raw`dir\\/a`,
        String.raw`dir\\/b`,
      ]);
    }
    const pattern = 'assets/{img,{css,js}}';
    assert.equal(
      braces.stringify(braces.parse(pattern), {escapeInvalid: true}),
      pattern,
    );
    assert.deepEqual(
      micromatch(
        ['docs/a.md', 'docs/b.mdx', 'docs/image.png'],
        '**/*.{md,mdx}',
      ),
      ['docs/a.md', 'docs/b.mdx'],
    );
  });

  for (const [location, entry] of Object.entries(lock.packages)) {
    if (!entry.dependencies?.braces) continue;
    const consumer = createRequire(
      path.join(project, location, 'package.json'),
    );
    test(`${location} resolves the guarded braces implementation`, () => {
      const dependency = consumer('braces');
      const pattern = '{'.repeat(maxDepth + 1) + 'x' + '}'.repeat(maxDepth + 1);
      assert.throws(() => bounded(() => dependency(pattern)), {
        name: 'SyntaxError',
        message: /exceeds max depth/,
      });
      assert.deepEqual(dependency.expand('docs/{api,cli}.md'), [
        'docs/api.md',
        'docs/cli.md',
      ]);
    });
  }
}

module.exports = registerTests;
if (require.main === module) registerTests(process.cwd());
