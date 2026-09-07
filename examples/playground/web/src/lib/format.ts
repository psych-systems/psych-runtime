import type { Cost, Usage } from "@/lib/types";

const numberFormatter = new Intl.NumberFormat("en-US");

export function formatTokens(n: number): string {
  return numberFormatter.format(n);
}

export function sumUsageTokens(usage: Usage): number {
  return usage.input + usage.output + usage.cache_read + usage.cache_write;
}

/**
 * Renders a `Cost | null` for display. `null` means the model had no known
 * price -- this returns "unknown", never "$0.00": a silent zero would make
 * metering look correct and be wrong.
 */
export function formatCost(cost: Cost | null): string {
  if (cost === null) return "unknown";
  const amount = Number(cost.amount);
  if (!Number.isFinite(amount)) return "unknown";
  const symbol = cost.currency === "USD" ? "$" : `${cost.currency} `;
  const digits = amount !== 0 && amount < 0.01 ? 4 : 2;
  return `${symbol}${amount.toFixed(digits)}`;
}

export function formatDuration(seconds: number | null): string {
  if (seconds === null) return "-";
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 2 : 1)}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  return `${minutes}m ${rest}s`;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatClockTime(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString(undefined, {
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

export function formatDayLabel(iso: string): string {
  const date = new Date(iso);
  const now = new Date();
  const startOfDay = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const diffDays = Math.round((startOfDay(now) - startOfDay(date)) / 86_400_000);

  if (diffDays === 0) return "Today";
  if (diffDays === 1) return "Yesterday";
  if (diffDays > 1 && diffDays < 7) {
    return date.toLocaleDateString(undefined, { weekday: "long" });
  }
  return date.toLocaleDateString(undefined, {
    month: "long",
    day: "numeric",
    year: date.getFullYear() === now.getFullYear() ? undefined : "numeric",
  });
}

export function truncate(text: string, max: number): string {
  if (text.length <= max) return text;
  return `${text.slice(0, max - 1).trimEnd()}…`;
}
