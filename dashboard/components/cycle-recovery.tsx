'use client';

import { useEffect, useRef, useState } from 'react';
import { CheckCheck } from 'lucide-react';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

type Pair = Record<'LONG' | 'SHORT', string>;
type Review = {
  token: string;
  symbol: string;
  checked_at: number;
  leverage: number;
  baseline: Pair;
  quantities: Pair;
  expected: Pair;
  actual: Pair;
  difference: Pair;
};

type Props = {
  accountId: string;
  accountName: string;
  disabled: boolean;
  action: (url: string, body?: object) => Promise<boolean>;
  setNotice: (message: string) => void;
};

export function CycleRecovery({
  accountId,
  accountName,
  disabled,
  action,
  setNotice,
}: Props) {
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [review, setReview] = useState<Review | null>(null);
  const [error, setError] = useState('');
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);

  async function readPositions() {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setReview(null);
    setError('');
    setOpen(true);
    setLoading(true);
    const timeout = setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch(
        `/api/accounts/${accountId}/cycle/recovery-preview`,
        {
          method: 'POST',
          signal: controller.signal,
        },
      );
      if (response.status === 404)
        throw new Error('服务器未提供核对入口，请更新并重启后端服务后重试');
      const data = (await response.json()) as Review & { detail?: string };
      if (!response.ok) throw new Error(data.detail || '读取失败，请重新核对');
      if (!controller.signal.aborted) setReview(data as Review);
    } catch (cause) {
      if (request.current === controller) {
        setError(
          controller.signal.aborted
            ? '读取超时，请重试'
            : cause instanceof Error
              ? cause.message
              : '读取失败',
        );
      }
    } finally {
      clearTimeout(timeout);
      if (request.current === controller) setLoading(false);
    }
  }

  async function confirm() {
    if (!review || loading || disabled) return;
    setLoading(true);
    const ok = await action(
      `/api/accounts/${accountId}/cycle/recovery-confirm`,
      { token: review.token },
    );
    setLoading(false);
    setReview(null);
    setOpen(false);
    if (ok)
      setNotice(
        '已核对：现有仓位已保留，本轮跟踪已结束。点击“启动账户”恢复运行。',
      );
  }

  return (
    <>
      <Button
        variant="outline"
        disabled={disabled || loading}
        onClick={() => void readPositions()}
      >
        <CheckCheck size={16} />
        已核对
      </Button>
      <Dialog
        open={open}
        onOpenChange={(value) => {
          if (!loading) setOpen(value);
        }}
      >
        <DialogContent
          className="cycle-recovery-dialog sm:max-w-xl"
          showCloseButton={!loading}
        >
          <DialogHeader>
            <DialogTitle>核对循环持仓 · {accountName}</DialogTitle>
            <DialogDescription>
              确认后结束本轮跟踪，将当前实际仓位作为原始持仓保留。
              本轮尚未减回的仓位也会保留，不再按旧计时自动平仓。成交历史保留，账户继续暂停。
            </DialogDescription>
          </DialogHeader>
          {loading && !review ? <output>正在读取交易所最新持仓…</output> : null}
          {error ? (
            <p role="alert" className="danger">
              {error}
            </p>
          ) : null}
          {review ? (
            <>
              <p>
                {review.symbol} · 实际杠杆 {review.leverage}x · 数量单位按该合约
              </p>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>数量</TableHead>
                    <TableHead>多头</TableHead>
                    <TableHead>空头</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {(
                    [
                      ['本轮原始', review.baseline],
                      ['本轮新增', review.quantities],
                      ['记录应有', review.expected],
                      ['当前实际', review.actual],
                      ['实际 − 记录', review.difference],
                    ] as const
                  ).map(([label, pair]) => (
                    <TableRow key={label}>
                      <TableCell>{label}</TableCell>
                      <TableCell>{pair.LONG}</TableCell>
                      <TableCell>{pair.SHORT}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
              <p className="muted">
                读取时间：
                {new Date(review.checked_at * 1000).toLocaleString('zh-CN', {
                  hour12: false,
                })}
                。确认时会再次核对；数据变化需重新读取。
              </p>
            </>
          ) : null}
          <DialogFooter>
            <Button
              variant="outline"
              disabled={loading}
              onClick={() => setOpen(false)}
            >
              取消
            </Button>
            <Button
              variant="outline"
              disabled={loading || disabled}
              onClick={() => void readPositions()}
            >
              重新读取
            </Button>
            <Button
              disabled={!review || loading || disabled}
              onClick={() => void confirm()}
            >
              {loading && review ? '正在确认…' : '确认已核对'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
