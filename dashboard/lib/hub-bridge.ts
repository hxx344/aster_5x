const CHANNEL = 'project-hub';
const MODULE_HOST = /^p-[a-f0-9]{24}\.hub\.localhost$/;

type BridgeCallbacks = {
  onActivity: (active: boolean) => void;
  onConnected: (connected: boolean) => void;
  onNavigate: (accountId?: string) => void;
};

export function connectHubBridge(target: Window, callbacks: BridgeCallbacks) {
  const embedded =
    target.parent !== target &&
    MODULE_HOST.test(target.location.hostname) &&
    ['http:', 'https:'].includes(target.location.protocol);
  const origin = `${target.location.protocol}//hub.localhost${target.location.port ? `:${target.location.port}` : ''}`;
  let connected = false;
  const send = (message: object) => {
    if (embedded)
      target.parent.postMessage(
        { channel: CHANNEL, version: 1, ...message },
        origin,
      );
  };
  const ready = () =>
    send({
      type: 'ready',
      role: 'module',
      capabilities: ['activity', 'changed', 'navigate'],
    });
  const receive = (event: MessageEvent) => {
    if (event.source !== target.parent || event.origin !== origin) return;
    const data = event.data;
    if (
      !data ||
      typeof data !== 'object' ||
      Array.isArray(data) ||
      data.channel !== CHANNEL ||
      data.version !== 1
    )
      return;
    if (data.type === 'ready' && data.role === 'host') {
      connected = true;
      callbacks.onConnected(true);
      ready();
    } else if (
      connected &&
      data.type === 'activity' &&
      typeof data.active === 'boolean'
    ) {
      callbacks.onActivity(data.active);
    } else if (
      connected &&
      data.type === 'navigate' &&
      data.projectId === 'aster'
    ) {
      const query = data.query;
      if (!query || typeof query !== 'object' || Array.isArray(query)) return;
      if (Object.keys(query).some((key) => key !== 'accountId')) return;
      if (
        query.accountId !== undefined &&
        (typeof query.accountId !== 'string' ||
          !/^[a-z0-9_-]{1,32}$/.test(query.accountId))
      )
        return;
      // Navigation changes only the viewed account; it never invokes actions.
      callbacks.onNavigate(query.accountId);
    }
  };
  if (embedded) {
    callbacks.onActivity(false);
    target.addEventListener('message', receive);
    // Also announce after hydration, in case iframe load preceded this effect.
    ready();
  }
  return {
    changed() {
      if (connected) send({ type: 'changed', scope: 'summary' });
    },
    openAssets() {
      if (connected) send({ type: 'navigate', projectId: 'asset', query: {} });
    },
    dispose() {
      if (embedded) target.removeEventListener('message', receive);
      connected = false;
    },
  };
}
