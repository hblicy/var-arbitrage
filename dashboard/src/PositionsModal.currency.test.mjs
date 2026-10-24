import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { runInNewContext } from 'node:vm';

const source = await readFile(new URL('./PositionsModal.jsx', import.meta.url), 'utf8');
const handler = source.slice(source.indexOf('const handleAdd = ') + 'const handleAdd = '.length,
  source.indexOf('    const handleDelete')).trim().replace(/;$/, '');

test('a QFEX native quote cannot be rewritten as USDT and posted to the dollar ledger', async () => {
  let requests = 0;
  let message = '';
  const add = runInNewContext(`(${handler})`, {
    newSymbol: 'SIVESEK', longEx: 'qfex', shortEx: 'popdex',
    entryQty: '1', entryPriceLong: '100', entryPriceShort: '100',
    getAuthHeader: () => '', setLoading: () => {}, setError: value => { message = value; },
    fetch: async () => {
      requests += 1;
      return { ok: false, json: async () => ({ detail: 'unexpected post' }) };
    },
  });
  await add({ preventDefault() {} });
  assert.equal(requests, 0);
  assert.match(message, /原币/);
});
