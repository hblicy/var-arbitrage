import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { runInNewContext } from 'node:vm';

const source = await readFile(new URL('./PositionsModal.jsx', import.meta.url), 'utf8');
const callback = source.match(/const loadPositions = useCallback\(([\s\S]*?)\r?\n    }, \[.*?\]\);/)[1] + '\n    }';

function setup() {
  const pending = [], errors = [], timers = new Map(), published = [];
  let nextTimer = 0;
  const context = {
    currentUser: { id: 'user', role: 'admin' }, tab: 'my-positions', filterOwner: '',
    getAuthHeader: () => 'test', handleLogout() {}, positionRequest: { current: null },
    AbortController, URLSearchParams, performance: { now: () => 1000 },
    setPositions: value => published.push(value), setPositionsReceivedAt() {}, setClock() {},
    setRefreshing() {}, setPositionError: value => errors.push(value), setError: value => errors.push(value),
    setTimeout: fn => { const id = ++nextTimer; timers.set(id, fn); return id; },
    clearTimeout: id => timers.delete(id), console: { error() {} },
    fetch: (url, options) => new Promise((resolve, reject) => {
      options.signal?.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })));
      pending.push({ url, signal: options.signal, finish: (status = 200, data = { held: {} }) =>
        resolve({ ok: status < 400, status, json: async () => data }) });
    }),
  };
  return { run: runInNewContext(`(${callback})`, context), context, pending, errors, timers, published };
}

test('a position poll does not overlap a slow request', async () => {
  const s = setup();
  const first = s.run(), second = s.run();
  assert.equal(s.pending.length, 1);
  s.pending[0].finish();
  await Promise.all([first, second]);
  assert.equal(s.published.length, 1);
  assert.equal(s.timers.size, 0);
});

test('manual refresh replaces the old request and keeps the replacement active', async () => {
  const s = setup();
  const first = s.run();
  const manual = s.run({ force: true });
  assert.equal(s.pending[0].signal.aborted, true);
  await first;
  const poll = s.run();
  assert.equal(s.pending.length, 2);
  s.pending[1].finish();
  await Promise.all([manual, poll]);
  assert.equal(s.published.length, 1);
  assert.equal(s.timers.size, 0);
});

test('switching the owner cancels the previous owner request', async () => {
  const s = setup();
  s.context.tab = 'all-positions';
  s.context.filterOwner = 'one';
  const first = s.run();
  s.context.filterOwner = 'two';
  const second = s.run();
  assert.equal(s.pending[0].signal.aborted, true);
  assert.match(s.pending[1].url, /owner=two/);
  s.pending[1].finish();
  await Promise.all([first, second]);
  assert.equal(s.published.length, 1);
});

test('a late JSON body from the old request cannot overwrite the replacement', async () => {
  const s = setup();
  let finishBody;
  const body = new Promise(resolve => { finishBody = resolve; });
  const first = s.run();
  s.pending[0].finish(200, body);
  await Promise.resolve();
  const manual = s.run({ force: true });
  s.pending[1].finish(200, { marker: 'new' });
  await manual;
  finishBody({ marker: 'old' });
  await first;
  assert.equal(s.published.length, 1);
  assert.equal(s.published[0].marker, 'new');
});

test('HTTP failures are visible and do not replace the previous snapshot', async () => {
  const s = setup();
  const request = s.run();
  s.pending[0].finish(503);
  await request;
  assert.equal(s.published.length, 0);
  assert.ok(s.errors.some(value => /503/.test(value)));
});

test('a timed-out price request reports the timeout and permits a retry', async () => {
  const s = setup();
  const first = s.run();
  assert.equal(s.timers.size, 1);
  [...s.timers.values()][0]();
  await first;
  assert.ok(s.errors.some(value => /超时/.test(value)));
  const second = s.run();
  s.pending[1].finish();
  await second;
  assert.equal(s.published.length, 1);
  assert.equal(s.timers.size, 0);
});
