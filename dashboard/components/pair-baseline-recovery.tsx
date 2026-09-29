'use client';

import { useEffect, useRef, useState } from 'react';
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
import type { DeskAction } from '@/lib/desk-types';
import type { Pair } from '@/lib/pairs';
import {
  pairBaselineRecoveryBlock,
  parsePairBaselineReview,
  type PairBaselineReview,
} from '@/lib/pair-order-recovery';

export function PairBaselineRecovery({
  pair,
  disabled,
  now,
  action,
  setNotice,
}: {
  pair: Pair;
  disabled: boolean;
  now: number;
  action: DeskAction;
  setNotice: (message: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [review, setReview] = useState<PairBaselineReview | null>(null);
  const [acknowledged, setAcknowledged] = useState(false);
  const [error, setError] = useState('');
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  const block = pairBaselineRecoveryBlock(pair);
  const locked = disabled || Boolean(block);
  const expired =
    review && (!Number.isFinite(now) || now >= review.checked_at + 300);

  async function read() {
    if (loading || locked) return;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setReview(null);
    setAcknowledged(false);
    setError('');
    setLoading(true);
    const timeout = setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch(`/api/pairs/${pair.id}/baseline-preview`, {
        method: 'POST',
        signal: controller.signal,
      });
      const data: unknown = await response.json();
      if (!response.ok) {
        const detail =
          data && typeof data === 'object' && 'detail' in data
            ? data.detail
            : null;
        throw new Error(
          typeof detail === 'string' ? detail : '底仓读取失败，请重新核对',
        );
      }
      if (!controller.signal.aborted && request.current === controller)
        setReview(parsePairBaselineReview(data, pair));
    } catch (cause) {
      if (request.current === controller)
        setError(
          controller.signal.aborted
            ? '读取超时，请重新核对'
            : cause instanceof Error
              ? cause.message
              : '底仓读取失败',
        );
    } finally {
      clearTimeout(timeout);
      if (request.current === controller) setLoading(false);
    }
  }

  async function confirm() {
    if (!review || !acknowledged || loading || locked || expired) return;
    setLoading(true);
    try {
      const ok = await action(`/api/pairs/${pair.id}/baseline-confirm`, {
        token: review.token,
        acknowledge_unknown: true,
      });
      setReview(null);
      setAcknowledged(false);
      setOpen(false);
      if (ok)
        setNotice(
          '已采纳当前底仓，实际仓位未改动；旧订单继续跟踪，配对组仍暂停，现在可点击启动。',
        );
    } finally {
      setLoading(false);
    }
  }

  return (
    <>
      <Button
        variant="outline"
        disabled={locked || loading}
        title={block || '核对归档旧订单并确认手动调整后的底仓'}
        onClick={() => {
          setReview(null);
          setAcknowledged(false);
          setError('');
          setOpen(true);
        }}
      >
        核对并采纳当前底仓
      </Button>
      <Dialog
        open={open}
        onOpenChange={(value) => {
          if (!loading) setOpen(value);
        }}
      >
        <DialogContent
          className="cycle-recovery-dialog sm:max-w-2xl"
          showCloseButton={!loading}
        >
          <DialogHeader>
            <DialogTitle>核对并采纳当前底仓 · {pair.name}</DialogTitle>
            <DialogDescription>
              人工归档后手动调整了仓位时，在这里确认当前底仓。采纳后保持暂停，不下单、不减仓、不划转。
            </DialogDescription>
          </DialogHeader>
          <p>
            将按原编号查询归档订单，并读取最新持仓与挂单。查不到的旧订单仍视为未知并继续跟踪；出现活动订单或迟到成交时，会阻止采纳。
          </p>
          {loading ? <output>正在核对旧订单与当前持仓…</output> : null}
          {error ? (
            <p role="alert" className="danger">
              {error}
            </p>
          ) : null}
          {review ? (
            <>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>方向</TableHead>
                    <TableHead>原底仓（XAU）</TableHead>
                    <TableHead>采纳数量（XAU）</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {(['LONG', 'SHORT'] as const).map((side) => (
                    <TableRow key={side}>
                      <TableCell>{side === 'LONG' ? '多' : '空'}</TableCell>
                      <TableCell>{review.before[side]}</TableCell>
                      <TableCell>{review.actual[side]}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
              <p>
                当前杠杆：{review.leverage}x。已查询 {review.orders.length}{' '}
                笔旧订单，其中{' '}
                {
                  review.orders.filter((order) => order.status === 'UNKNOWN')
                    .length
                }{' '}
                笔仍未取得回执，其余均为零成交终态。原始归档记录全部保留。
              </p>
              <label className="pair-order-recovery-ack">
                <input
                  type="checkbox"
                  checked={acknowledged}
                  disabled={loading || locked}
                  onChange={(event) => setAcknowledged(event.target.checked)}
                />
                我已在交易所核对当前持仓，确认以上数量作为底仓，并接受未知旧订单继续跟踪。
              </label>
              {expired ? (
                <p role="alert" className="amber">
                  预览已过期，请重新核对。
                </p>
              ) : null}
            </>
          ) : null}
          {block ? (
            <p role="alert" className="amber">
              {block}
            </p>
          ) : null}
          <DialogFooter>
            <Button
              variant="outline"
              disabled={loading}
              onClick={() => setOpen(false)}
            >
              关闭
            </Button>
            <Button
              variant="outline"
              disabled={loading || locked}
              onClick={() => void read()}
            >
              {review ? '重新核对' : '读取订单与当前持仓'}
            </Button>
            {review ? (
              <Button
                disabled={
                  !acknowledged || loading || locked || Boolean(expired)
                }
                onClick={() => void confirm()}
              >
                确认采纳，保持暂停
              </Button>
            ) : null}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
