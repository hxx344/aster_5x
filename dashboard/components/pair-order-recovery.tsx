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
  parsePairOrderRecoveryCheck,
  type PairOrderRecoveryCheck,
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
  // Keep completion visible when reconciliation clears the pending batch.
  // Archive confirmation separately checks the current pair and batch identity.
  return <PairOrderRecoveryDialog key={props.pair.id} {...props} />;
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
  const [checked, setChecked] = useState<PairOrderRecoveryCheck | null>(null);
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

  async function readOrdersAndPositions(archive = false) {
    if (locked || loading) return;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setReview(null);
    setChecked(null);
    setReceiptsFound('');
    setAcknowledged(false);
    setError('');
    setOpen(true);
    setLoading(true);
    const timeout = setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch(
        `/api/pairs/${pair.id}/${archive ? 'recovery-preview' : 'recovery-check'}`,
        {
          method: 'POST',
          signal: controller.signal,
        },
      );
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
      if (archive) {
        const result = parsePairOrderRecovery(data, pair);
        if (result.status === 'receipts_found')
          setReceiptsFound(result.message);
        else setReview(result);
      } else {
        setChecked(parsePairOrderRecoveryCheck(data, pair));
      }
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
              按原订单编号查询未确认回执，并按成交记录核对两侧实际持仓。已有回执会继续参与核对。
            </DialogDescription>
          </DialogHeader>
          <p className="muted">
            本次手动核对不会提交新订单或划转。核对完成后仍保持暂停，需手动启动配对组。
          </p>
          <p className="muted">
            普通开仓原订单与补偿订单均结束、账户核验通过后，两侧数量一致则保留为底仓，包括手动增加的仓位；数量不一致且与本批回执吻合时，后台才回退本批新增量。
          </p>
          <p className="muted">
            暂停后可直接点击“启动配对组”自动核对，无需先使用本入口，也无需减回旧底仓。原成交记录继续保留。
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
          {checked ? (
            <div className="grid gap-3">
              <output>{checked.message}</output>
              <p className="muted">核对时间：{dateTime(checked.checked_at)}</p>
              {checked.orders.map((order) => (
                <div key={order.client_order_id}>
                  <p>
                    {order.side === 'LONG' ? 'A · 只多' : 'B · 只空'} ·
                    {order.status === 'UNKNOWN'
                      ? '尚无有效回执'
                      : `回执状态 ${order.status}`}{' '}
                    · 累计成交 {order.executed_qty ?? '未知'}
                  </p>
                  <p className="muted break-all">
                    CID：<code>{order.client_order_id}</code>
                  </p>
                  {order.error ? (
                    <p className="amber">本单反馈：{order.error}</p>
                  ) : null}
                </div>
              ))}
              <p className="muted">{checked.archive_reason}</p>
              {checked.completed ? (
                <p>本批跟踪已完成，配对组仍保持暂停。关闭后可启动配对组。</p>
              ) : null}
            </div>
          ) : null}
          {review ? (
            <>
              {review.message ? <p>{review.message}</p> : null}
              <p className="muted">
                查不到不等于从未受理。仅在两侧实际仓位等于本批前底仓、无挂单等条件全部满足时允许人工归档。
                保留旧批次，之后若发现迟到成交将阻止新开仓和划转。
              </p>
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
              关闭
            </Button>
            {!checked?.completed ? (
              <Button
                variant="outline"
                disabled={loading || locked}
                onClick={() => void readOrdersAndPositions()}
              >
                重新核对
              </Button>
            ) : null}
            {checked?.archive_available ? (
              <Button
                variant="outline"
                disabled={
                  loading ||
                  locked ||
                  checked.batch_id !== pair.state?.pending?.id
                }
                onClick={() => void readOrdersAndPositions(true)}
              >
                检查人工归档条件
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
