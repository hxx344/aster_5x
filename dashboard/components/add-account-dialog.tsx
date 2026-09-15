'use client';
import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Plus } from 'lucide-react';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';
import type { DeskAction } from '@/lib/desk-types';
export function AddAccountDialog({
  busy,
  action,
  selectAccount,
  available,
}: {
  busy: boolean;
  action: DeskAction;
  selectAccount: (id: string) => void;
  available: boolean;
}) {
  const [addOpen, setAddOpen] = useState(false);
  const [newAccount, setNewAccount] = useState({
    id: '',
    name: '',
    env_prefix: '',
    mode: 'live',
  });
  return (
    <Dialog open={addOpen} onOpenChange={setAddOpen}>
      <DialogTrigger
        render={<Button variant="outline" disabled={!available || busy} />}
      >
        <Plus size={16} />
        添加账户
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>添加子账户</DialogTitle>
        </DialogHeader>
        <form
          className="form-stack"
          onSubmit={async (e) => {
            e.preventDefault();
            if (await action('/api/accounts', newAccount)) {
              setAddOpen(false);
              selectAccount(newAccount.id);
              setNewAccount({
                id: '',
                name: '',
                env_prefix: '',
                mode: 'live',
              });
            }
          }}
        >
          <label htmlFor="account-name">
            账户名称
            <Input
              id="account-name"
              required
              maxLength={50}
              value={newAccount.name}
              onChange={(e) =>
                setNewAccount({ ...newAccount, name: e.target.value })
              }
              placeholder="主策略子账户"
            />
          </label>
          <label htmlFor="account-id">
            账户标识
            <Input
              id="account-id"
              required
              pattern={'[a-z0-9_\\-]{1,32}'}
              value={newAccount.id}
              onChange={(e) =>
                setNewAccount({ ...newAccount, id: e.target.value })
              }
              placeholder="sub01"
            />
          </label>
          <label htmlFor="account-prefix">
            凭据环境变量前缀
            <Input
              id="account-prefix"
              required
              pattern="[A-Z][A-Z0-9_]{1,40}"
              value={newAccount.env_prefix}
              onChange={(e) =>
                setNewAccount({
                  ...newAccount,
                  env_prefix: e.target.value.toUpperCase(),
                })
              }
              placeholder="ASTER_SUB01"
            />
          </label>
          <p className="muted">只填写变量前缀。API 签名密钥由服务器读取。</p>
          <Select
            value={newAccount.mode}
            onValueChange={(v) =>
              v && setNewAccount({ ...newAccount, mode: v })
            }
          >
            <SelectTrigger aria-label="执行环境">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="live">实盘账户</SelectItem>
              <SelectItem value="paper">模拟账户</SelectItem>
            </SelectContent>
          </Select>
          <Button disabled={busy} type="submit">
            保存账户，保持暂停
          </Button>
        </form>
      </DialogContent>
    </Dialog>
  );
}
