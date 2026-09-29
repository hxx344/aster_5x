'use client';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { CycleTradesPanel } from '@/components/cycle-trades-panel';
import { ExecutionEvents } from '@/components/execution-events';
import { CycleQualityHistoryPanel } from '@/components/cycle-quality-history';
import type { Account } from '@/lib/desk-types';
import type { ExecutionEvent } from '@/lib/cycle-events';
import type { Pair } from '@/lib/pairs';
export function RecordsWorkspace({
  account,
  pair,
  events,
  now,
  stale,
}: {
  account: Account;
  pair?: Pair;
  events: ExecutionEvent[];
  now: number;
  stale: boolean;
}) {
  return (
    <Tabs defaultValue="trades" className="feature-stack">
      <TabsList aria-label="记录类型">
        <TabsTrigger value="trades">循环成交</TabsTrigger>
        <TabsTrigger value="events">执行记录</TabsTrigger>
        <TabsTrigger value="quality">成交质量</TabsTrigger>
      </TabsList>
      <TabsContent value="trades">
        <CycleTradesPanel
          key={account.id}
          accountName={account.name}
          trades={account.cycle_trades}
          reportStatus={account.cycle_state?.report_status}
          now={now}
          stale={stale}
        />
      </TabsContent>
      <TabsContent value="events">
        <section className="panel">
          <ExecutionEvents key={account.id} events={events} />
        </section>
      </TabsContent>
      <TabsContent value="quality">
        <CycleQualityHistoryPanel
          key={pair ? `pair:${pair.id}` : account.id}
          accountName={pair ? `配对组 ${pair.name}` : account.name}
          scope={pair ? 'pair' : undefined}
          quality={
            pair
              ? pair.state?.execution_quality
              : account.cycle_state?.execution_quality
          }
          history={
            pair
              ? pair.state?.execution_quality_history
              : account.cycle_state?.execution_quality_history
          }
          stale={stale}
        />
      </TabsContent>
    </Tabs>
  );
}
