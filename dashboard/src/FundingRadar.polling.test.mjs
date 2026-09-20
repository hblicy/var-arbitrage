import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { runInNewContext } from 'node:vm';

for (const [file, name, ref] of [['FundingRadar.jsx', 'refresh', 'scanAbort'], ['App.jsx', 'fetchData', 'abortControllerRef']]) {
  const source = await readFile(new URL(`./${file}`, import.meta.url), 'utf8');
  const callback = source.match(new RegExp(`const ${name} = useCallback\\(([\\s\\S]*?)\\r?\\n  }, \\[\\]\\);`))[1] + '\n  }';
  function setup() {
    const pending = [], errors = [], timers = new Map();
    let published = 0;
    const context = {
      [ref]: { current: null }, AbortController, performance: { now: () => 0 },
      setPacket: () => published++, setData: () => published++, setError: value => errors.push(value),
      normalizeDashboardData: value => value, console: { error() {} },
      setTimeout: fn => { const id = timers.size + 1; timers.set(id, fn); return id; },
      clearTimeout: id => timers.delete(id),
      fetch: (_url, { signal }) => new Promise((resolve, reject) => {
        signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })));
        pending.push({ signal, finish: () => resolve({ ok: true, json: async () => ({}) }) });
      }),
    };
    return { run: runInNewContext(`(${callback})`, context), pending, errors, timers, published: () => published };
  }

  test(`${file}: automatic poll waits for slow in-flight response`, async () => {
    const s = setup();
    const first = s.run();
    const poll = s.run();
    assert.equal(s.pending[0].signal.aborted, false);
    assert.equal(s.pending.length, 1);
    s.pending[0].finish();
    await Promise.all([first, poll]);
    assert.equal(s.published(), 1);
    const next = s.run();
    assert.equal(s.pending.length, 2);
    s.pending[1].finish();
    await next;
    assert.equal(s.published(), 2);
    assert.equal(s.timers.size, 0);
  });

  test(`${file}: manual refresh supersedes old request without releasing the new one`, async () => {
    const s = setup();
    const first = s.run();
    const manual = s.run({ force: true });
    assert.equal(s.pending[0].signal.aborted, true);
    await first;
    const poll = s.run();
    assert.equal(s.pending.length, 2);
    assert.equal(s.pending[1].signal.aborted, false);
    s.pending[1].finish();
    await Promise.all([manual, poll]);
    assert.equal(s.published(), 1);
  });

  test(`${file}: timed-out request reports failure and allows retry`, async () => {
    const s = setup();
    const first = s.run();
    assert.equal(s.timers.size, 1);
    [...s.timers.values()][0]();
    await first;
    assert.ok(s.errors.some(Boolean));
    const next = s.run();
    assert.equal(s.pending.length, 2);
    s.pending[1].finish();
    await next;
    assert.equal(s.published(), 1);
    assert.equal(s.timers.size, 0);
  });
}
