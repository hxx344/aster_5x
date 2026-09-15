export type AccountCapacity = {
  leverage: number;
  cap: string;
  occupied: string;
  remaining: string;
  checked_at: number;
  expires_at: number;
};

export function accountCapacityView(
  value: AccountCapacity | undefined,
  leverage: number | undefined,
  now: number,
  disconnected: boolean,
) {
  if (!value || value.leverage !== leverage)
    return { ready: false, detail: '等待账户档位' };
  if (
    disconnected ||
    !Number.isFinite(value.checked_at) ||
    !Number.isFinite(value.expires_at) ||
    now < value.checked_at - 1 ||
    now >= value.expires_at
  )
    return { ready: false, detail: '账户数据过期' };
  if (
    [value.cap, value.occupied, value.remaining].some(
      (amount) =>
        amount.trim() === '' ||
        !Number.isFinite(Number(amount)) ||
        Number(amount) < 0,
    )
  )
    return { ready: false, detail: '等待有效账户档位' };
  return { ready: true, detail: '账户余量' };
}
