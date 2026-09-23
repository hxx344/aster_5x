import type { CycleDiagnostic } from './cycle';
import { isDisplayTimestamp } from './display-time.ts';

function diagnosticText(value: unknown, fallback = '—'): string {
  return typeof value === 'string' && value.trim() ? value : fallback;
}

export function cycleDiagnosticView(
  diagnostic: CycleDiagnostic | null | undefined,
) {
  if (!diagnostic || typeof diagnostic !== 'object') return null;
  const timestamp = diagnostic.checked_at;
  const checkedAt = isDisplayTimestamp(timestamp) ? timestamp : undefined;
  return {
    title: diagnosticText(diagnostic.title, '循环检查未通过'),
    symbol: diagnosticText(diagnostic.symbol, '品种未知'),
    phase:
      diagnostic.phase === 'open'
        ? '开仓'
        : diagnostic.phase === 'close'
          ? '平仓'
          : '阶段未知',
    checkedAt,
    checks: Array.isArray(diagnostic.checks)
      ? diagnostic.checks
          .map((check, index) => ({
            key: `${diagnosticText(check?.code, 'check')}:${index}`,
            label: diagnosticText(check?.label, '检查项目'),
            actual: diagnosticText(check?.actual),
            required: diagnosticText(check?.required),
            unit: diagnosticText(check?.unit, ''),
            status:
              check?.passed === true
                ? '已满足'
                : check?.passed === false
                  ? '未满足'
                  : '未核验',
            tone:
              check?.passed === true
                ? 'mint'
                : check?.passed === false
                  ? 'amber'
                  : 'muted',
            priority:
              check?.passed === false ? 0 : check?.passed === true ? 2 : 1,
          }))
          .sort((left, right) => left.priority - right.priority)
      : [],
    context: Array.isArray(diagnostic.context)
      ? diagnostic.context.map((item, index) => ({
          key: `${diagnosticText(item?.label, 'context')}:${index}`,
          label: diagnosticText(item?.label, '检查上下文'),
          value: diagnosticText(item?.value),
          unit: diagnosticText(item?.unit, ''),
        }))
      : [],
    note: diagnosticText(diagnostic.note, ''),
  };
}
