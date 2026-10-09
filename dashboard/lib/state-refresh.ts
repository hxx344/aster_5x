type RefreshPoller = {
  setActivity: (active: boolean) => void;
  cancel: () => void;
  refresh: () => Promise<void>;
};

// A read permission is separate from mutation/session pauses inside the poller.
export function createStateRefresh({
  target,
  page,
  poller,
  onTick,
}: {
  target: Window;
  page: Document;
  poller: RefreshPoller;
  onTick: () => void;
}) {
  let hostActive = true;
  let backgroundUpdates = true;
  let timer: ReturnType<typeof setInterval> | undefined;
  let disposed = false;
  const tick = () => {
    onTick();
    void poller.refresh();
  };
  const update = () => {
    if (disposed) return;
    clearInterval(timer);
    const foreground = !page.hidden && hostActive;
    const allowed =
      target.navigator.onLine && (foreground || backgroundUpdates);
    poller.cancel();
    poller.setActivity(allowed);
    if (allowed) {
      tick();
      timer = setInterval(tick, foreground ? 3000 : 30_000);
    }
  };
  page.addEventListener('visibilitychange', update);
  for (const event of ['online', 'offline', 'focus', 'pageshow'])
    target.addEventListener(event, update);
  return {
    start: update,
    setHostActivity(active: boolean, background = false) {
      hostActive = active;
      backgroundUpdates = background;
      update();
    },
    dispose() {
      disposed = true;
      clearInterval(timer);
      page.removeEventListener('visibilitychange', update);
      for (const event of ['online', 'offline', 'focus', 'pageshow'])
        target.removeEventListener(event, update);
      poller.setActivity(false);
    },
  };
}
