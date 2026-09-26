'use client';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { CycleTradesPanel } from '@/components/cycle-trades-panel';
import { ExecutionEvents } from '@/components/execution-events';
import { CycleQualityHistoryPanel } from '@/components/cycle-quality-history';
import type { Account } from '@/lib/desk-types';
import type { ExecutionEvent } from '@/lib/cycle-events';
export function RecordsWorkspace({
  account,
  events,
  now,
  stale,
}: {
  account: Account;
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
          key={account.id}
          accountName={account.name}
          quality={account.cycle_state?.execution_quality}
          history={account.cycle_state?.execution_quality_history}
          stale={stale}
        />
      </TabsContent>
    </Tabs>
  );
}
