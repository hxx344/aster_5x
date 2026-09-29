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
import type { DeskAction } from '@/lib/desk-types';
import type { Pair } from '@/lib/pairs';
import {
  pairOrderRecoveryBlock,
  pairOrderRecoveryReviewBlock,
  parsePairOrderRecovery,
  type PairOrderRecoveryReview,
} from '@/lib/pair-order-recovery';

type Props = {
  pair: Pair;
  disabled: boolean;
  now: number;
  action: DeskAction;
  setNotice: (message: string) => void;
};

function dateTime(seconds: number): string {
  return new Date(seconds * 1000).toLocaleString('zh-CN', { hour12: false });
}

export function PairOrderRecovery(props: Props) {
  // Changing pair or batch must discard the old token and abort its preview.
  return (
    <PairOrderRecoveryDialog
      key={JSON.stringify([props.pair.id, props.pair.state?.pending?.id])}
      {...props}
    />
  );
}

function PairOrderRecoveryDialog({
  pair,
  disabled,
  now,
  action,
  setNotice,
}: Props) {
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [review, setReview] = useState<PairOrderRecoveryReview | null>(null);
  const [receiptsFound, setReceiptsFound] = useState('');
  const [acknowledged, setAcknowledged] = useState(false);
  const [error, setError] = useState('');
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  const block = pairOrderRecoveryBlock(pair);
  const locked = disabled || Boolean(block);
  const reviewBlock = review
    ? pairOrderRecoveryReviewBlock(review, pair, now)
    : '';

  async function readOrdersAndPositions() {
    if (locked || loading) return;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setReview(null);
    setReceiptsFound('');
    setAcknowledged(false);
    setError('');
    setOpen(true);
    setLoading(true);
    const timeout = setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch(`/api/pairs/${pair.id}/recovery-preview`, {
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
          typeof detail === 'string'
            ? detail
            : response.status === 404
              ? '服务器未提供核对入口，请更新并重启后端服务后重试'
              : '读取失败，请重新核对',
        );
      }
      if (controller.signal.aborted || request.current !== controller) return;
      const result = parsePairOrderRecovery(data, pair);
      if (result.status === 'receipts_found') setReceiptsFound(result.message);
      else setReview(result);
    } catch (cause) {
      if (request.current === controller) {
        setError(
          controller.signal.aborted
            ? '读取超时，请重试'
            : cause instanceof Error
              ? cause.message
              : '读取失败，请重新核对',
        );
      }
    } finally {
      clearTimeout(timeout);
      if (request.current === controller) setLoading(false);
    }
  }

  async function confirm() {
    if (!review || !acknowledged || loading || locked || reviewBlock) return;
    setLoading(true);
    try {
      const ok = await action(`/api/pairs/${pair.id}/recovery-confirm`, {
        token: review.token,
        acknowledge_unknown: true,
      });
      setReview(null);
      setAcknowledged(false);
      setOpen(false);
      if (ok)
        setNotice(
          '本批跟踪已归档，底仓与旧批次记录保留，配对组仍保持暂停。核对后可点击“启动配对组”。',
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
        title={block || '核对普通开仓待确认订单与两侧实际持仓'}
        onClick={() => void readOrdersAndPositions()}
      >
        <CheckCheck size={16} />
        核对订单与持仓
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
            <DialogTitle>核对订单与持仓 · {pair.name}</DialogTitle>
            <DialogDescription>
              查不到不等于从未受理。仅在两侧实际仓位等于本批前底仓、无挂单等条件全部满足时允许人工归档。
              保留旧批次，之后若发现迟到成交将阻止新开仓和划转。
            </DialogDescription>
          </DialogHeader>
          <p className="muted">
            核对不会提交新订单或划转。确认归档后仍保持暂停，需手动启动配对组。
          </p>
          {loading && !review ? (
            <output>正在查询原订单并读取交易所最新持仓…</output>
          ) : null}
          {error ? (
            <p role="alert" className="danger">
              {error}
            </p>
          ) : null}
          {receiptsFound ? <output>{receiptsFound}</output> : null}
          {review ? (
            <>
              {review.message ? <p>{review.message}</p> : null}
              <p className="muted break-all">原批次：{review.batch_id}</p>
              <p className="muted">创建时间：{dateTime(review.created_at)}</p>
              <div className="grid gap-3">
                {review.orders.map((order) => (
                  <div key={order.side}>
                    <p>
                      {order.side === 'LONG' ? 'A · 只多' : 'B · 只空'} ·
                      原订单暂未查到
                    </p>
                    <p className="muted break-all">
                      原 CID：<code>{order.client_order_id}</code>
                    </p>
                  </div>
                ))}
              </div>
              <p>
                {pair.symbol} · 实际杠杆 {review.leverage}x · 数量单位按该合约
              </p>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>数量</TableHead>
                    <TableHead>A · 多头</TableHead>
                    <TableHead>B · 空头</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  <TableRow>
                    <TableCell>本批前底仓</TableCell>
                    <TableCell>{review.before.LONG}</TableCell>
                    <TableCell>{review.before.SHORT}</TableCell>
                  </TableRow>
                  <TableRow>
                    <TableCell>新核对实际持仓</TableCell>
                    <TableCell>{review.actual.LONG}</TableCell>
                    <TableCell>{review.actual.SHORT}</TableCell>
                  </TableRow>
                </TableBody>
              </Table>
              <p className="muted">
                核对时间：{dateTime(review.checked_at)}。预览 5
                分钟内有效，确认时会再次核对；状态变化需重新读取。
              </p>
              {reviewBlock ? (
                <p className="amber" role="alert">
                  {reviewBlock}
                </p>
              ) : null}
              <label className="pair-order-recovery-ack">
                <input
                  type="checkbox"
                  checked={acknowledged}
                  disabled={loading || locked || Boolean(reviewBlock)}
                  onChange={(event) => setAcknowledged(event.target.checked)}
                />
                <span>我已在交易所核对订单与持仓，确认结束本批跟踪</span>
              </label>
            </>
          ) : null}
          <DialogFooter className="sm:flex-wrap">
            <Button
              variant="outline"
              disabled={loading}
              onClick={() => setOpen(false)}
            >
              {receiptsFound ? '关闭' : '取消'}
            </Button>
            {!receiptsFound ? (
              <Button
                variant="outline"
                disabled={loading || locked}
                onClick={() => void readOrdersAndPositions()}
              >
                重新读取
              </Button>
            ) : null}
            {review ? (
              <Button
                disabled={
                  !acknowledged || loading || locked || Boolean(reviewBlock)
                }
                onClick={() => void confirm()}
              >
                {loading ? '正在确认…' : '确认归档，保留底仓'}
              </Button>
            ) : null}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
