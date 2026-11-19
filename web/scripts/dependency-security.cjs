'use strict';

const path = require('node:path');

require('../../scripts/test-braces-security.cjs')(
  path.resolve(__dirname, '..'),
);
require('../../scripts/test-picomatch-security.cjs')(path.resolve(__dirname, '..'));
