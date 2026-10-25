import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { runInNewContext } from 'node:vm';

const source = await readFile(new URL('./App.jsx', import.meta.url), 'utf8');
const definitions = source.slice(source.indexOf('const STORAGE_KEY'), source.indexOf('const EMPTY_DASHBOARD_DATA'));
const initializer = source.match(/const \[exchangeStates, setExchangeStates\] = useState\((.+)\);/)[1];

function initialStates(saved) {
  return runInNewContext(`${definitions}\nconst initial = ${initializer}; typeof initial === 'function' ? initial() : initial`, {
    localStorage: { getItem: () => saved }, console: { warn() {} },
  });
}

test('exchange controls exist before the first server response and retain saved choices', () => {
  const states = initialStates('{"binance":false,"qfex":true}');
  assert.equal(states.length, 10);
  assert.equal(states.find(ex => ex.name === 'binance').enabled, false);
  assert.equal(states.find(ex => ex.name === 'qfex').enabled, true);
  assert.equal(states.find(ex => ex.name === 'popdex').enabled, true);
});

test('invalid saved preferences cannot remove the exchange controls', () => {
  for (const saved of ['null', '[]', 'broken json', '{"qfex":"false"}']) {
    assert.equal(initialStates(saved).length, 10);
    assert.equal(initialStates(saved).find(ex => ex.name === 'qfex').enabled, true);
  }
});

test('old Bulk preferences cannot restore the removed exchange or position option', async () => {
  assert.equal(initialStates('{"bulk":true}').some(ex => ex.name === 'bulk'), false);
  const positions = await readFile(new URL('./PositionsModal.jsx', import.meta.url), 'utf8');
  const expression = positions.match(/const POSITION_EXCHANGES = ([^;]+);/)[1];
  const exchanges = runInNewContext(expression);
  assert.equal(exchanges.length, 10);
  assert.equal(exchanges.includes('bulk'), false);
});

function setup(responses, saved = '{}') {
  const callback = source.match(/const fetchExchanges = useCallback\(([\s\S]*?)\r?\n  }, \[\]\);/);
  assert.ok(callback, 'exchange requests need a reusable retry callback');
  const errors = [], states = [], pending = [], timers = new Map();
  const loaded = { current: false }, controller = { current: null };
  const context = {
    localStorage: { getItem: () => saved }, console: { warn() {}, error() {} },
    exchangeLoadedRef: loaded, exchangeAbortRef: controller, AbortController,
    setExchangeError: value => errors.push(value), setExchangeStates: value => states.push(value),
    setTimeout: fn => { const id = timers.size + 1; timers.set(id, fn); return id; },
    clearTimeout: id => timers.delete(id),
    fetch: (_url, { signal }) => {
      const next = responses.shift();
      if (next) return next();
      return new Promise((resolve, reject) => {
        signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })));
        pending.push({ signal, resolve });
      });
    },
  };
  return { run: runInNewContext(`${definitions}\n(${callback[1]}\n})`, context),
    errors, states, timers, loaded, controller, pending };
}

const success = () => Promise.resolve({ ok: true, json: async () => [{ name: 'qfex' }, { name: 'popdex' }] });

test('a failed initial request reports an error and can recover without reloading the page', async () => {
  const s = setup([() => Promise.resolve({ ok: false, status: 503 }), success], '{"qfex":false}');
  await s.run();
  assert.equal(s.loaded.current, false);
  assert.match(s.errors.at(-1), /交易所/);
  assert.equal(s.states.length, 0);
  await s.run();
  assert.equal(s.loaded.current, true);
  assert.equal(s.errors.at(-1), null);
  assert.equal(s.states.at(-1).find(ex => ex.name === 'qfex').enabled, false);
  assert.equal(s.timers.size, 0);
});

test('timed out exchange requests release the pending request and allow retry', async () => {
  const s = setup([]);
  const first = s.run();
  await s.run();
  assert.equal(s.pending.length, 1);
  [...s.timers.values()][0]();
  await first;
  assert.equal(s.loaded.current, false);
  assert.match(s.errors.at(-1), /超时/);
  const retry = s.run();
  assert.equal(s.pending.length, 2);
  s.pending[1].resolve(await success());
  await retry;
  assert.equal(s.loaded.current, true);
});

test('malformed exchange responses report failure and keep the current controls', async () => {
  const s = setup([() => Promise.resolve({ ok: true, json: async () => ({ detail: 'error' }) })]);
  await s.run();
  assert.equal(s.loaded.current, false);
  assert.equal(s.states.length, 0);
  assert.ok(s.errors.at(-1));
});
