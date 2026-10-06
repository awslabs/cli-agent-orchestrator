'use strict';

const assert = require('node:assert/strict');
const {createRequire} = require('node:module');
const path = require('node:path');
const {test} = require('node:test');
const {SourceMapConsumer} = require('source-map-js');
const selectorParser = require('postcss-selector-parser');
const CachePolicy = createRequire(require.resolve('cacheable-request'))(
  'http-cache-semantics',
);

require('../../scripts/test-braces-security.cjs')(
  path.resolve(__dirname, '..'),
);

const request = {
  url: 'https://example.com/resource',
  method: 'GET',
  headers: {},
};

for (const directive of ['private, max-age=60', 'no-store']) {
  test(`a shared cache policy refuses storage of ${directive} responses`, () => {
    const policy = new CachePolicy(
      request,
      {
        status: 200,
        headers: {'cache-control': directive, 'set-cookie': 'session=example'},
      },
      {shared: true},
    );
    assert.equal(policy.storable(), false);
  });
}

test('explicitly public stale responses still obey a permitted max-stale request', () => {
  const policy = new CachePolicy(
    request,
    {
      status: 200,
      headers: {'cache-control': 'public, max-age=1'},
    },
    {shared: true},
  );
  const received = policy.now();
  policy.now = () => received + 2000;
  assert.equal(policy.storable(), true);
  assert.equal(policy.satisfiesWithoutRevalidation(request), false);
  assert.equal(
    policy.satisfiesWithoutRevalidation({
      ...request,
      headers: {'cache-control': 'max-stale=10'},
    }),
    true,
  );
});

test('a wildcard anywhere in Vary prevents reuse', () => {
  const policy = new CachePolicy(request, {
    status: 200,
    headers: {
      'cache-control': 'public, max-age=60',
      vary: 'Accept-Encoding, *',
    },
  });
  assert.equal(policy.satisfiesWithoutRevalidation(request), false);
});

function indexedMap(
  line,
  map = {
    version: 3,
    sources: ['input.js'],
    names: [],
    mappings: 'AAAA',
  },
) {
  return {
    version: 3,
    sections: [{offset: {line, column: 0}, map}],
  };
}

test('indexed source maps preserve ordinary generated positions and sources', () => {
  const consumer = new SourceMapConsumer(indexedMap(5));
  const mappings = [];
  consumer.eachMapping(({generatedLine, originalLine, source}) => {
    mappings.push({generatedLine, originalLine, source});
  });
  assert.deepEqual(mappings, [
    {generatedLine: 6, originalLine: 1, source: 'input.js'},
  ]);
  assert.deepEqual(consumer.sources, ['input.js']);
});

test('indexed source maps bound direct and aggregate nested line offsets', () => {
  assert.doesNotThrow(() => new SourceMapConsumer(indexedMap(10_000_000)));
  for (const map of [
    indexedMap(10_000_001),
    indexedMap(6_000_000, indexedMap(4_000_001)),
  ]) {
    assert.throws(
      () => new SourceMapConsumer(map),
      /Section offset line must not exceed/,
    );
  }
});

test('indexed source maps reject invalid line and column offsets', () => {
  for (const field of ['line', 'column']) {
    for (const value of [-1, 0.5, Infinity]) {
      const map = indexedMap(0);
      map.sections[0].offset[field] = value;
      assert.throws(
        () => new SourceMapConsumer(map),
        /must be non-negative integers/,
      );
    }
  }
});

for (const [selector, classes] of [
  ['.card > .item:hover', ['card', 'item']],
  ['.item\\:active:is([data-label="a,b"], .other)', ['item:active', 'other']],
]) {
  test(`the selector-parser override preserves ${selector}`, () => {
    const ast = selectorParser().astSync(selector);
    const actual = [];
    ast.walkClasses((node) => actual.push(node.value));
    assert.deepEqual(actual, classes);
    assert.equal(ast.toString(), selector);
  });
}

test(
  'Tinypool preserves the Docusaurus worker-thread contract',
  {timeout: 10_000},
  async (t) => {
    const {default: Tinypool} = await import('tinypool');
    const pool = new Tinypool({
      filename: path.join(__dirname, 'fixtures', 'tinypool-worker.cjs'),
      minThreads: 2,
      maxThreads: 2,
      concurrentTasksPerWorker: 1,
      runtime: 'worker_threads',
      isolateWorkers: false,
      workerData: {params: {increment: 3}},
    });
    t.after(() => pool.destroy());
    const results = await Promise.all(
      [2, -4].map((value) => pool.run({value})),
    );
    assert.deepEqual(
      results.map((result) => result.value),
      [5, -1],
    );
    for (const result of results) {
      assert.ok(Number.isInteger(result.workerId) && result.workerId > 0);
    }
  },
);
