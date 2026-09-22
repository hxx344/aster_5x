import assert from 'node:assert/strict';
import { test } from 'node:test';
import { connectHubBridge } from '../lib/hub-bridge.ts';

function fixture({
  hostname = `p-${'a'.repeat(24)}.hub.localhost`,
  embedded = true,
  protocol = 'http:',
} = {}) {
  const sent = [],
    activities = [],
    connections = [],
    navigations = [];
  let receive;
  const parent = {
    postMessage: (message, origin) => sent.push({ message, origin }),
  };
  const target = {
    location: { hostname, protocol, port: '18080' },
    parent,
    addEventListener: (name, listener) => {
      assert.equal(name, 'message');
      receive = listener;
    },
    removeEventListener: (name, listener) => {
      assert.equal(name, 'message');
      assert.equal(listener, receive);
      receive = undefined;
    },
  };
  if (!embedded) target.parent = target;
  const bridge = connectHubBridge(target, {
    onActivity: (value) => activities.push(value),
    onConnected: (value) => connections.push(value),
    onNavigate: (value) => navigations.push(value),
  });
  const message = (data, override = {}) =>
    receive?.({
      data: { channel: 'project-hub', version: 1, ...data },
      source: parent,
      origin: `${protocol}//hub.localhost:18080`,
      ...override,
    });
  return { bridge, sent, activities, connections, navigations, message };
}

test('isolated hub module starts paused and completes a validated host handshake', () => {
  const f = fixture();
  assert.deepEqual(f.activities, [false]);
  assert.equal(f.sent[0].origin, 'http://hub.localhost:18080');
  assert.deepEqual(f.sent[0].message.capabilities, [
    'activity',
    'changed',
    'navigate',
  ]);
  f.message({ type: 'activity', active: true });
  assert.deepEqual(f.activities, [false]);
  f.message({ type: 'ready', role: 'host' });
  assert.deepEqual(f.connections, [true]);
  f.message({ type: 'activity', active: true });
  f.message({ type: 'activity', active: false });
  assert.deepEqual(f.activities, [false, true, false]);
  f.bridge.dispose();
  f.message({ type: 'activity', active: true });
  assert.deepEqual(f.activities, [false, true, false]);
});

test('wrong source, origin, channel and version cannot activate an iframe', () => {
  const f = fixture();
  for (const origin of [
    'http://hub.localhost',
    'https://hub.localhost:18080',
    'http://evil.hub.localhost:18080',
    'http://hub.localhost:18080.evil.test',
    'null',
  ])
    f.message({ type: 'ready', role: 'host' }, { origin });
  f.message({ type: 'ready', role: 'host' }, { source: {} });
  f.message({ type: 'ready', role: 'host', channel: 'another-channel' });
  f.message({ type: 'ready', role: 'host', version: 2 });
  assert.deepEqual(f.connections, []);
  assert.deepEqual(f.activities, [false]);
  f.bridge.changed();
  f.bridge.openAssets();
  assert.equal(f.sent.length, 1);
});

test('standalone or noncanonical hosts neither message parent nor suspend polling', () => {
  for (const options of [
    { embedded: false },
    { hostname: 'aster.example.com' },
    { hostname: 'p-abc.hub.localhost' },
    { hostname: `p-${'a'.repeat(24)}.hub.localhost.evil.test` },
    { protocol: 'file:' },
  ]) {
    const f = fixture(options);
    f.message({ type: 'ready', role: 'host' });
    f.bridge.changed();
    f.bridge.openAssets();
    assert.deepEqual(f.sent, []);
    assert.deepEqual(f.activities, []);
    f.bridge.dispose();
  }
});

test('changed is explicit and asset navigation sends no account credentials or address', () => {
  const f = fixture();
  f.message({ type: 'ready', role: 'host' });
  f.message({ type: 'activity', active: true });
  assert.equal(
    f.sent.filter((row) => row.message.type === 'changed').length,
    0,
  );
  f.bridge.changed();
  f.bridge.openAssets();
  assert.deepEqual(f.sent.at(-2).message, {
    channel: 'project-hub',
    version: 1,
    type: 'changed',
    scope: 'summary',
  });
  assert.deepEqual(f.sent.at(-1).message, {
    channel: 'project-hub',
    version: 1,
    type: 'navigate',
    projectId: 'asset',
    query: {},
  });
});

test('host navigation can only request a viewed account, never an action or URL', () => {
  const f = fixture();
  f.message({ type: 'ready', role: 'host' });
  for (const data of [
    { projectId: 'asset', query: {} },
    { projectId: 'aster', query: { action: 'enable' } },
    { projectId: 'aster', query: { accountId: '../api/accounts/test/enable' } },
    {
      projectId: 'aster',
      query: { accountId: 'test', url: '/api/accounts/test/enable' },
    },
    { projectId: 'aster', query: [] },
  ])
    f.message({ type: 'navigate', ...data });
  assert.deepEqual(f.navigations, []);
  f.message({
    type: 'navigate',
    projectId: 'aster',
    query: { accountId: 'test-2' },
  });
  f.message({ type: 'navigate', projectId: 'aster', query: {} });
  assert.deepEqual(f.navigations, ['test-2', undefined]);
});
