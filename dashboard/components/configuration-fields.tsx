'use client';
import type { ComponentProps, ReactNode } from 'react';
import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  submitConfiguration,
  type ConfigurationSubmission,
} from '@/lib/configuration-submit';

export function SettingsForm({
  children,
  ...submission
}: ConfigurationSubmission & { children: ReactNode }) {
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        void submitConfiguration(submission);
      }}
    >
      {children}
    </form>
  );
}

export function ConfigurationField({
  id,
  label,
  unit,
  onValueChange,
  items,
  display,
  children,
  value,
  ...input
}: Omit<ComponentProps<'input'>, 'onChange' | 'value'> & {
  id: string;
  label: string;
  unit?: string;
  value: string | null;
  onValueChange: (value: string) => void;
  items?: readonly (readonly [string, string])[];
  display?: ReactNode;
}) {
  return (
    <label htmlFor={id}>
      {label}
      {unit ? (
        <>
          {' '}
          <span>{unit}</span>
        </>
      ) : null}
      {items ? (
        <Select
          value={value}
          disabled={input.disabled}
          required={input.required}
          onValueChange={(next) => next && onValueChange(next)}
        >
          <SelectTrigger id={id} className="full-width">
            <SelectValue placeholder={input.placeholder}>{display}</SelectValue>
          </SelectTrigger>
          <SelectContent>
            {items.map(([key, text]) => (
              <SelectItem key={key} value={key}>
                {text}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      ) : (
        <Input
          id={id}
          type="number"
          min="0"
          step="any"
          required
          {...input}
          value={value ?? ''}
          onChange={(event) => onValueChange(event.target.value)}
        />
      )}
      {children}
    </label>
  );
}
