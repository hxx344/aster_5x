'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import type { State } from './desk-types';
import type { CycleTrade } from './cycle';
import { createStatePoller } from './state-poller';
import { createStateHistory } from './state-history';
export function useTradingDesk() {
  const [state, setState] = useState<State | null>(null);
  const [selected, setSelected] = useState('');
  const [history] = useState(() => createStateHistory<CycleTrade>());
  const [error, setError] = useState('');
  const [errorAccountId, setErrorAccountId] = useState('');
  const [connectionError, setConnectionError] = useState('');
  const [needsLogin, setNeedsLogin] = useState(false);
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [serverClock, setServerClock] = useState({ server: 0, local: 0 });
  const [now, setNow] = useState(0);
  const [notice, setNotice] = useState('');
  const operationPending = useRef(false);
  const clearSession = useCallback(() => {
    setNeedsLogin(true);
    setState(null);
    setSelected('');
    history.clear();
    setServerClock({ server: 0, local: 0 });
    setConnectionError('');
    setError('');
    setErrorAccountId('');
    setNotice('');
    setPassword('');
  }, [history]);
  const [poller] = useState(() =>
    createStatePoller<State>({
      requestUrl: () => history.url(),
      onState: (next) => {
        setState({ ...next, accounts: history.merge(next.accounts) });
        setServerClock({ server: next.updated_at, local: Date.now() / 1000 });
        setNeedsLogin(false);
        setConnectionError('');
        setSelected(history.selected());
      },
      onUnauthorized: clearSession,
      onError: setConnectionError,
    }),
  );
  const selectAccount = (id: string) => {
    history.select(id);
    setSelected(id);
    poller.pause();
    poller.resume();
    void poller.refresh();
  };
  const refresh = useCallback(() => poller.refresh(), [poller]);
  useEffect(() => {
    poller.resume();
    const tick = () => {
      setNow(Date.now() / 1000);
      void refresh();
    };
    const initial = setTimeout(tick, 0);
    const timer = setInterval(tick, 3000);
    return () => {
      clearTimeout(initial);
      clearInterval(timer);
      poller.pause();
    };
  }, [refresh, poller]);
  const action = async (
    url: string,
    body?: object,
    method: 'POST' | 'PATCH' = 'POST',
  ) => {
    if (operationPending.current) return false;
    operationPending.current = true;
    poller.pause();
    let resumePolling = true;
    setBusy(true);
    setError('');
    setErrorAccountId(selected);
    setNotice('');
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 30000);
    try {
      const r = await fetch(url, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {}),
        signal: controller.signal,
      });
      const data = (await r.json().catch(() => ({}))) as { detail?: string };
      if (controller.signal.aborted) throw new Error('操作响应超时');
      clearTimeout(timeout);
      if (r.status === 401) {
        clearSession();
        resumePolling = false;
      }
      if (!r.ok) throw new Error(data.detail || '操作未完成');
      if (url === '/api/logout') {
        clearSession();
        resumePolling = false;
      } else {
        poller.resume();
        resumePolling = false;
        await refresh();
      }
      return true;
    } catch (e) {
      setError(
        controller.signal.aborted
          ? '操作结果暂未确认，请核对最新状态后再操作'
          : e instanceof Error
            ? e.message
            : '操作失败',
      );
      return false;
    } finally {
      clearTimeout(timeout);
      operationPending.current = false;
      if (resumePolling) {
        poller.resume();
        void refresh();
      }
      setBusy(false);
    }
  };
  const serverNow = serverClock.server + Math.max(0, now - serverClock.local);
  return {
    state,
    selected,
    selectAccount,
    error,
    setError,
    errorAccountId,
    connectionError,
    needsLogin,
    password,
    setPassword,
    busy,
    now,
    serverNow,
    notice,
    setNotice,
    action,
    refresh: () => {
      if (!operationPending.current) void poller.refresh({ resume: true });
    },
  };
}
