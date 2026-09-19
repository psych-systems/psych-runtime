"use client";

import type { ReactNode } from "react";

import { Input } from "@/components/ui/input";
import { LabelWithHelp } from "@/components/ui/help";
import { cn } from "@/lib/utils";

/**
 * One labelled number, with its explanation behind the "?" beside the label.
 *
 * The form this replaces put a sentence under every box, so a screen of
 * fourteen limits was fourteen boxes and fourteen paragraphs. The words did
 * not get worse; they moved.
 */
export function NumberField({
  id,
  label,
  help,
  value,
  onChange,
  min,
  max,
  step,
  error,
  placeholder,
  suffix,
}: {
  id: string;
  label: string;
  help?: ReactNode;
  /** Empty string is a real value for an optional number. */
  value: number | string;
  onChange: (next: string) => void;
  min?: number;
  max?: number;
  step?: number;
  error?: string;
  placeholder?: string;
  /** A unit shown after the label, e.g. "seconds". */
  suffix?: string;
}) {
  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <LabelWithHelp
        htmlFor={id}
        label={suffix ? `${label} (${suffix})` : label}
        help={help}
      />
      <Input
        id={id}
        type="number"
        className="tabular"
        min={min}
        max={max}
        step={step}
        value={value}
        placeholder={placeholder}
        aria-invalid={error !== undefined}
        onChange={(event) => onChange(event.target.value)}
      />
      {error && <p className="text-caption text-status-failed">{error}</p>}
    </div>
  );
}

/** The grid every block of advanced fields uses. One column on a phone. */
export function FieldGrid({
  children,
  className,
  columns = 2,
}: {
  children: ReactNode;
  className?: string;
  columns?: 2 | 3;
}) {
  return (
    <div
      className={cn(
        "grid grid-cols-1 gap-4",
        columns === 2 ? "sm:grid-cols-2" : "sm:grid-cols-2 lg:grid-cols-3",
        className,
      )}
    >
      {children}
    </div>
  );
}
