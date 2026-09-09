import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createStatePoller } from '../lib/state-poller.ts';

function deferred() {
  let resolve, reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

function fixture(t) {
  const calls = [], states = [], errors = [], unauthorized = [];
  const poller = createStatePoller({
    request: (_url, options) => {
      const call = { ...deferred(), signal: options.signal };
      calls.push(call);
      return call.promise;
    },
    onState: (state) => states.push(state),
    onError: (message) => errors.push(message),
    onUnauthorized: () => unauthorized.push(true),
  });
  t.after(() => poller.pause());
  return { poller, calls, states, errors, unauthorized };
}

test('slow polling has at most one request in flight', async (t) => {
  const f = fixture(t);
  const pending = f.poller.refresh();
  await Promise.all(Array.from({ length: 20 }, () => f.poller.refresh()));
  assert.equal(f.calls.length, 1);
  f.calls[0].resolve(Response.json({ version: 1 }));
  await pending;
  assert.deepEqual(f.states, [{ version: 1 }]);
  const next = f.poller.refresh();
  assert.equal(f.calls.length, 2);
  f.calls[1].resolve(Response.json({ version: 2 }));
  await next;
});

test('mutation cancels old state and an obsolete completion cannot release the new slot', async (t) => {
  const f = fixture(t);
  const old = f.poller.refresh();
  f.poller.pause();
  assert.equal(f.calls[0].signal.aborted, true);
  await f.poller.refresh();
  assert.equal(f.calls.length, 1);
  f.poller.resume();
  const current = f.poller.refresh();
  f.calls[0].resolve(Response.json({ version: 'before mutation' }));
  await old;
  await f.poller.refresh();
  assert.equal(f.calls.length, 2);
  assert.deepEqual(f.states, []);
  f.calls[1].resolve(Response.json({ version: 'after mutation' }));
  await current;
  assert.deepEqual(f.states, [{ version: 'after mutation' }]);
});

test('logout discards a response whose body is still being read', async (t) => {
  const f = fixture(t);
  const body = deferred();
  const pending = f.poller.refresh();
  f.calls[0].resolve({ ok: true, status: 200, json: () => body.promise });
  await Promise.resolve();
  f.poller.pause();
  body.resolve({ accounts: ['private account'] });
  await pending;
  assert.deepEqual(f.states, []);
  assert.deepEqual(f.errors, []);
});

test('unauthorized response suspends polling until a new login', async (t) => {
  const f = fixture(t);
  const pending = f.poller.refresh();
  f.calls[0].resolve(new Response(null, { status: 401 }));
  await pending;
  await f.poller.refresh();
  assert.equal(f.calls.length, 1);
  assert.deepEqual(f.unauthorized, [true]);
  f.poller.resume();
  const login = f.poller.refresh();
  f.calls[1].resolve(Response.json({ accounts: [] }));
  await login;
  assert.deepEqual(f.states, [{ accounts: [] }]);
});

test('timeout releases polling and ignores even an uncancellable late response', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const f = fixture(t);
  const old = f.poller.refresh();
  t.mock.timers.tick(10000);
  assert.equal(f.calls[0].signal.aborted, true);
  assert.equal(f.errors.length, 1);
  const current = f.poller.refresh();
  f.calls[0].resolve(Response.json({ stale: true }));
  await old;
  t.mock.timers.tick(10000);
  assert.equal(f.calls[1].signal.aborted, true);
  assert.equal(f.errors.length, 2);
  f.calls[1].reject(new Error('aborted'));
  await current;
  assert.deepEqual(f.states, []);
  assert.equal(f.errors.length, 2);
});

test('network failure permits a subsequent refresh', async (t) => {
  const f = fixture(t);
  const failed = f.poller.refresh();
  f.calls[0].reject(new Error('offline'));
  await failed;
  const retry = f.poller.refresh();
  f.calls[1].resolve(Response.json({ recovered: true }));
  await retry;
  assert.deepEqual(f.errors, ['offline']);
  assert.deepEqual(f.states, [{ recovered: true }]);
});
