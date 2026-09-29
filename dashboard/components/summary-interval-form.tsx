'use client';
import { useId, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { submitSummaryInterval } from '@/lib/summary-interval';

export function SummaryIntervalForm({
  savedMinutes,
  disabled,
  save,
}: {
  savedMinutes: number;
  disabled: boolean;
  save: (body: { hourly_summary_interval_minutes: number }) => Promise<boolean>;
}) {
  const id = useId();
  const [draft, setDraft] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  // The server value follows polling only until the user starts editing.
  const value = draft ?? String(savedMinutes);

  return (
    <form
      className="monitor-setting monitor-summary-interval"
      noValidate
      onSubmit={async (event) => {
        event.preventDefault();
        if (disabled) return;
        setError('');
        setNotice('');
        try {
          const minutes = await submitSummaryInterval({
            value,
            disabled,
            save,
          });
          if (minutes == null) {
            setError('保存未完成，输入已保留，请核对错误提示后重试。');
            return;
          }
          setDraft(null);
          setNotice(`已保存：每 ${minutes} 分钟发送一次。`);
        } catch (error) {
          setError(
            error instanceof Error ? error.message : '保存未完成，请重试。',
          );
        }
      }}
    >
      <div>
        <label htmlFor={id}>
          <strong>摘要间隔（分钟）</strong>
        </label>
        <p id={`${id}-help`}>支持 1–1440 分钟，输入后点击保存。</p>
      </div>
      <div className="monitor-summary-field">
        <div className="monitor-summary-controls">
          <Input
            id={id}
            type="number"
            min={1}
            max={1440}
            step={1}
            required
            value={value}
            disabled={disabled}
            aria-invalid={Boolean(error)}
            aria-describedby={`${id}-help ${id}-status`}
            onChange={(event) => {
              setDraft(event.target.value);
              setError('');
              setNotice('');
            }}
          />
          <Button type="submit" disabled={disabled}>
            保存间隔
          </Button>
        </div>
        <output
          id={`${id}-status`}
          className="monitor-summary-feedback"
          aria-live="polite"
        >
          {error || notice || '当前输入未提交前，不影响发送安排。'}
        </output>
      </div>
    </form>
  );
}
