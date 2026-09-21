'use client';

import { useState } from 'react';
import { Trash2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import type { FeatureProps } from '@/lib/desk-types';

export function DeleteAccountDialog({
  account,
  busy,
  action,
  setNotice,
  connectionError,
}: Omit<FeatureProps, 'setError'> & { connectionError: string }) {
  const [open, setOpen] = useState(false);
  const reason = connectionError
    ? '连接恢复后可删除账户'
    : account.enabled
      ? '请先暂停账户，再删除'
      : account.deletion_block === undefined
        ? '服务尚未提供删除状态，请更新服务后重试'
        : account.deletion_block;
  const disabled = busy || Boolean(reason);
  return (
    <section className="panel">
      <div className="section-head">
        <h2>删除账户</h2>
        <Trash2 size={18} className="danger" />
      </div>
      <p className="muted">
        从工作台移除账户，保留本地历史记录。交易所持仓和服务器 API 凭据保留。
      </p>
      {reason && <p className="small-note amber">{reason}</p>}
      <Button
        variant="destructive"
        disabled={disabled}
        onClick={() => setOpen(true)}
      >
        <Trash2 size={16} /> 删除账户
      </Button>
      <Dialog open={open} onOpenChange={(value) => !busy && setOpen(value)}>
        <DialogContent showCloseButton={!busy}>
          <DialogHeader>
            <DialogTitle>删除账户“{account.name}”？</DialogTitle>
            <DialogDescription>
              账户标识：{account.id}
              。删除后停止管理此账户，不会平仓。历史记录保留，重新添加需使用新的账户标识。
            </DialogDescription>
          </DialogHeader>
          {reason && <p className="small-note amber">{reason}</p>}
          <DialogFooter>
            <Button
              variant="outline"
              disabled={busy}
              onClick={() => setOpen(false)}
            >
              取消
            </Button>
            <Button
              variant="destructive"
              disabled={disabled}
              onClick={async () => {
                const deleted = await action(
                  `/api/accounts/${account.id}`,
                  undefined,
                  'DELETE',
                );
                setOpen(false);
                if (deleted)
                  setNotice(`账户“${account.name}”已删除，历史记录已保留`);
              }}
            >
              确认删除
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}
