/** "3 min ago", for a list of conversations.
 *
 * A history list is scanned, not read: an exact timestamp there makes a person
 * do the subtraction themselves. `Intl.RelativeTimeFormat` keeps it in the
 * viewer's own language rather than in hand-written English. */
const RELATIVE = new Intl.RelativeTimeFormat(undefined, { numeric: "auto", style: "narrow" });

const STEPS: [seconds: number, unit: Intl.RelativeTimeFormatUnit][] = [
  [60, "second"],
  [3600, "minute"],
  [86_400, "hour"],
  [604_800, "day"],
  [2_629_800, "week"],
  [31_557_600, "month"],
];

export function relativeTime(iso: string, now: number = Date.now()): string {
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return "";
  const elapsed = (then - now) / 1000;
  const magnitude = Math.abs(elapsed);
  if (magnitude < 45) return "just now";

  let divisor = 1;
  for (const [limit, unit] of STEPS) {
    if (magnitude < limit) return RELATIVE.format(Math.round(elapsed / divisor), unit);
    divisor = limit;
  }
  return RELATIVE.format(Math.round(elapsed / 31_557_600), "year");
}
