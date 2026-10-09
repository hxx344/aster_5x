import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createStatePoller } from '../lib/state-poller.ts';
import { createStateRefresh } from '../lib/state-refresh.ts';

function fixture(t) {
  t.mock.timers.enable({ apis: ['setInterval', 'setTimeout'] });
  const target = new EventTarget(),
    page = new EventTarget();
  target.navigator = { onLine: true };
  page.hidden = false;
  const calls = [],
    states = [];
  const poller = createStatePoller({
    request: (_url, { signal }) =>
      new Promise((resolve) => calls.push({ signal, resolve })),
    onState: (state) => states.push(state),
    onUnauthorized() {},
    onError() {},
  });
  const updates = createStateRefresh({ target, page, poller, onTick() {} });
  t.after(() => updates.dispose());
  const finish = async (value = calls.length, index = calls.length - 1) => {
    calls[index].resolve(Response.json({ value }));
    for (let i = 0; i < 8; i++) await Promise.resolve();
  };
  return { target, page, calls, states, poller, updates, finish };
}

test('hidden standalone page keeps reading every thirty seconds and returns to foreground cadence', async (t) => {
  const f = fixture(t);
  f.page.hidden = true;
  f.updates.start();
  await f.finish();
  for (let i = 0; i < 3; i++) {
    t.mock.timers.tick(29_999);
    assert.equal(f.calls.length, i + 1);
    t.mock.timers.tick(1);
    await f.finish();
  }
  assert.deepEqual(
    f.states.map((state) => state.value),
    [1, 2, 3, 4],
  );
  f.page.hidden = false;
  f.page.dispatchEvent(new Event('visibilitychange'));
  await f.finish();
  t.mock.timers.tick(3000);
  assert.equal(f.calls.length, 6);
});

test('embedded page requires host permission, legacy inactivity stops reads, and opt-in keeps them running', async (t) => {
  const f = fixture(t);
  f.updates.setHostActivity(false);
  f.updates.start();
  t.mock.timers.tick(60_000);
  assert.equal(f.calls.length, 0);
  f.updates.setHostActivity(false, true);
  await f.finish();
  t.mock.timers.tick(30_000);
  await f.finish();
  assert.equal(f.calls.length, 2);
  f.updates.setHostActivity(false);
  t.mock.timers.tick(60_000);
  assert.equal(f.calls.length, 2);
});

test('wake events replace hung reads, ignore late responses and do not bypass mutation or session pauses', async (t) => {
  const f = fixture(t);
  f.updates.start();
  for (const event of ['focus', 'pageshow', 'online']) {
    const old = f.calls.length - 1;
    f.target.dispatchEvent(new Event(event));
    assert.equal(f.calls[old].signal.aborted, true);
    await f.finish('current');
    await f.finish('obsolete', old);
    assert.equal(f.states.at(-1).value, 'current');
    assert(!f.states.some((state) => state.value === 'obsolete'));
    void f.poller.refresh();
  }
  f.poller.pause();
  const count = f.calls.length;
  f.target.dispatchEvent(new Event('focus'));
  t.mock.timers.tick(30_000);
  assert.equal(f.calls.length, count);
  f.poller.resume();
  f.target.dispatchEvent(new Event('focus'));
  f.calls.at(-1).resolve(new Response(null, { status: 401 }));
  for (let i = 0; i < 8; i++) await Promise.resolve();
  f.target.dispatchEvent(new Event('pageshow'));
  t.mock.timers.tick(30_000);
  assert.equal(f.calls.length, count + 1);
});

test('offline and disposal cancel reads and remove wake listeners', async (t) => {
  const f = fixture(t);
  f.updates.start();
  f.target.navigator.onLine = false;
  f.target.dispatchEvent(new Event('offline'));
  assert.equal(f.calls[0].signal.aborted, true);
  t.mock.timers.tick(60_000);
  assert.equal(f.calls.length, 1);
  f.target.navigator.onLine = true;
  f.target.dispatchEvent(new Event('online'));
  await f.finish();
  assert.equal(f.calls.length, 2);
  f.updates.dispose();
  f.target.dispatchEvent(new Event('focus'));
  t.mock.timers.tick(60_000);
  assert.equal(f.calls.length, 2);
});
