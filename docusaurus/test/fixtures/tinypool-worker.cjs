'use strict';

const {workerData} = require('node:worker_threads');

module.exports = ({value}) => ({
  value: value + workerData[1].params.increment,
  workerId: process.__tinypool_state__.workerId,
});
