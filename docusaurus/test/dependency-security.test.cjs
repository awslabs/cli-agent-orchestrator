'use strict';

const assert = require('node:assert/strict');
const {createRequire} = require('node:module');
const path = require('node:path');
const {test} = require('node:test');
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
