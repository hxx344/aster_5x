import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  parseSummaryInterval,
  submitSummaryInterval,
  summaryIntervalError,
} from '../lib/summary-interval.ts';

test('summary interval accepts integer minute bounds and rejects empty, fractional or out-of-range input', () => {
  for (const [text, minutes] of [
    ['1', 1],
    ['60', 60],
    ['1440', 1440],
    [' 30 ', 30],
  ])
    assert.equal(parseSummaryInterval(text), minutes);

  for (const text of ['', ' ', '0', '-1', '1.5', '1441', 'Infinity', 'NaN'])
    assert.throws(() => parseSummaryInterval(text), {
      message: summaryIntervalError,
    });
});

test('invalid and disabled submissions never send a monitoring update', async () => {
  const requests = [];
  const save = async (body) => {
    requests.push(body);
    return true;
  };
  assert.equal(
    await submitSummaryInterval({ value: '', disabled: true, save }),
    null,
  );
  await assert.rejects(
    submitSummaryInterval({ value: '1.5', disabled: false, save }),
    { message: summaryIntervalError },
  );
  assert.deepEqual(requests, []);
});

test('a save returns the acknowledged interval as a number; rejected saves cannot clear the draft', async () => {
  const requests = [];
  for (const accepted of [false, true]) {
    assert.equal(
      await submitSummaryInterval({
        value: '25',
        disabled: false,
        save: async (body) => {
          requests.push(body);
          return accepted;
        },
      }),
      accepted ? 25 : null,
    );
  }
  assert.deepEqual(requests, [
    { hourly_summary_interval_minutes: 25 },
    { hourly_summary_interval_minutes: 25 },
  ]);
});

test('transport failures propagate to the form without acknowledgement', async () => {
  await assert.rejects(
    submitSummaryInterval({
      value: '60',
      disabled: false,
      save: async () => {
        throw new Error('offline');
      },
    }),
    { message: 'offline' },
  );
});
