import type { FeatureProps } from './desk-types';

export type ConfigurationSubmission = Pick<
  FeatureProps,
  'account' | 'action' | 'setError' | 'setNotice'
> & {
  locked: boolean;
  changes: () => object;
  clearDraft: () => void;
  success: string;
  errorFallback: string;
  clearNoticeOnError?: boolean;
};

export async function submitConfiguration(settings: ConfigurationSubmission) {
  if (settings.locked) return;
  try {
    if (
      await settings.action(
        `/api/accounts/${settings.account.id}`,
        settings.changes(),
        'PATCH',
      )
    ) {
      settings.clearDraft();
      settings.setNotice(settings.success);
    }
  } catch (error) {
    if (settings.clearNoticeOnError !== false) settings.setNotice('');
    settings.setError(
      error instanceof Error ? error.message : settings.errorFallback,
    );
  }
}
