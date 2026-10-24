import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { runInNewContext } from 'node:vm';

const source = await readFile(new URL('./App.jsx', import.meta.url), 'utf8');
const callback = source.match(/const fetchData = useCallback\(([\s\S]*?)\r?\n  }, \[\]\);/)[1] + '\n}';
const valid = { markets: {}, opportunities: [], last_update: 'fresh', data_version: 2 };
const response = (status, json = async () => valid, contentType = 'application/json') => ({
  status, ok: status >= 200 && status < 300, json, headers: { get: () => contentType },
});

function setup(replies) {
  let data = { markets: { BTCUSDT: {} }, opportunities: [], last_update: 'previous' };
  const errors = [], logs = [], timers = new Map();
  const context = {
    abortControllerRef: { current: null }, AbortController, performance: { now: () => 100 },
    setData: update => { data = typeof update === 'function' ? update(data) : update; },
    setError: value => errors.push(value), normalizeDashboardData: value => value,
    getDashboardSignature: value => JSON.stringify(value),
    console: { error: (...args) => logs.push(args) },
    setTimeout: fn => { const id = timers.size+1; timers.set(id, fn); return id; },
    clearTimeout: id => timers.delete(id),
    fetch: (_url, { signal }) => {
      const next = replies.shift();
      if (next) return next();
      return new Promise((_resolve, reject) => signal.addEventListener('abort',
        () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' }))));
    },
  };
  return { run: runInNewContext(`(${callback})`, context), errors, logs, timers, data: () => data };
}

test('HTTP failures preserve their status and the previous data, then recover on retry', async () => {
  for (const status of [500, 502, 503, 504]) {
    const s = setup([async () => response(status), async () => response(200)]);
    await s.run();
    assert.match(s.errors.at(-1), new RegExp(`HTTP ${status}`));
    assert.equal(s.data().last_update, 'previous');
    assert.equal(s.logs.at(-1)[1].status, status);
    assert.equal(s.logs.at(-1)[1].url, '/api/data');
    await s.run({ force: true });
    assert.equal(s.errors.at(-1), null);
    assert.equal(s.data().last_update, 'fresh');
  }
});

test('invalid JSON and wrong response shapes are distinct from a network failure', async () => {
  for (const json of [async () => { throw new SyntaxError('unexpected HTML'); },
    async () => ({ detail: 'unexpected proxy response' }), async () => null]) {
    const s = setup([async () => response(200, json)]);
    await s.run();
    assert.match(s.errors.at(-1), /响应格式/);
    assert.match(s.errors.at(-1), /HTTP 200/);
    assert.equal(s.data().last_update, 'previous');
    assert.equal(s.logs.at(-1)[1].status, 200);
  }
});

test('a body download interruption is reported after receipt of HTTP headers', async () => {
  const s = setup([async () => response(200, async () => { throw new TypeError('body terminated'); })]);
  await s.run();
  assert.match(s.errors.at(-1), /响应读取失败/);
  assert.equal(s.logs.at(-1)[1].status, 200);
  assert.equal(s.data().last_update, 'previous');
});

test('a transport failure is reported as network trouble without claiming api.py stopped', async () => {
  const s = setup([async () => { throw new TypeError('Failed to fetch'); }]);
  await s.run();
  assert.match(s.errors.at(-1), /网络/);
  assert.doesNotMatch(s.errors.at(-1), /api\.py/);
  assert.equal(s.data().last_update, 'previous');
});

test('a timed out request reports its timeout and permits the next retry', async () => {
  const s = setup([]);
  const pending = s.run();
  [...s.timers.values()][0]();
  await pending;
  assert.match(s.errors.at(-1), /超时/);
  assert.equal(s.logs.at(-1)[1].url, '/api/data');
  assert.equal(s.timers.size, 0);
  assert.equal(s.data().last_update, 'previous');
});
