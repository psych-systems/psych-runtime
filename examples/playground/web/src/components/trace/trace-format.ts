/** Small formatting helpers specific to the trace view, kept separate from
 * `src/lib/format.ts` because they format positions on a timeline rather
 * than values from a report field. */

/** `12.4s` / `1m 03s`, prefixed with `T+` for a timeline offset. `null`
 * means "not timed" (see `trace-model.ts`), and says so rather than
 * fabricating a zero offset. */
export function formatOffset(seconds: number | null): string {
  if (seconds === null) return "not timed";
  const clamped = Math.max(0, seconds);
  if (clamped < 60) return `T+${clamped.toFixed(clamped < 10 ? 2 : 1)}s`;
  const minutes = Math.floor(clamped / 60);
  const rest = (clamped % 60).toFixed(0).padStart(2, "0");
  return `T+${minutes}m ${rest}s`;
}

export function clampFraction(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.min(1, Math.max(0, value));
}
