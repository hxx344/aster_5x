import type { Account } from './desk-types';
import { cycleHasPosition } from './cycle';

export function accountConfigurationLock(account: Account): string {
  if (cycleHasPosition(account.cycle_state))
    return '本轮循环新增仓位减回、批次核对完成并暂停后可修改设置。';
  if (
    account.cycle_state?.active_batch ||
    account.migration_state?.active_batch ||
    account.status === 'reconciling'
  )
    return '当前批次核对完成并暂停账户后可修改设置。';
  return account.enabled ? '暂停账户后可修改设置。' : '';
}
