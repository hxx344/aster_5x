type PollerOptions<T> = {
  onState: (state: T) => void;
  onUnauthorized: () => void;
  onError: (message: string) => void;
  request?: typeof fetch;
  timeoutMs?: number;
};

export function createStatePoller<T>({
  onState,
  onUnauthorized,
  onError,
  request = fetch,
  timeoutMs = 10000,
}: PollerOptions<T>) {
  let paused = false;
  let active: AbortController | null = null;
  let timer: ReturnType<typeof setTimeout> | undefined;

  const cancel = () => {
    const previous = active;
    active = null;
    clearTimeout(timer);
    previous?.abort();
  };

  return {
    pause() {
      paused = true;
      cancel();
    },
    resume() {
      paused = false;
    },
    async refresh() {
      if (paused || active) return;
      const controller = new AbortController();
      active = controller;
      timer = setTimeout(() => {
        if (active !== controller) return;
        cancel();
        onError('连接超时，请稍后重试');
      }, timeoutMs);
      try {
        const response = await request('/api/state', {
          cache: 'no-store',
          signal: controller.signal,
        });
        if (active !== controller) return;
        if (response.status === 401) {
          paused = true;
          onUnauthorized();
          return;
        }
        if (!response.ok) {
          const body = (await response.json().catch(() => ({}))) as { detail?: string };
          throw new Error(body.detail || '交易服务暂时不可用');
        }
        const next: T = await response.json();
        if (active === controller) onState(next);
      } catch (error) {
        if (active === controller)
          onError(error instanceof Error ? error.message : '连接失败');
      } finally {
        // An obsolete request must not clear a newer request's timer or slot.
        if (active === controller) {
          clearTimeout(timer);
          active = null;
        }
      }
    },
  };
}
