"use client";

import { useEffect, useState } from "react";

/**
 * A ticking "12s elapsed" readout for a run in progress. `crash-recovery`
 * and `four-stores` run tens of seconds against real processes and real
 * databases with no progress frame in between some of those seconds -- this
 * is what tells a reader the page is waiting on real work, not stuck.
 * Stops ticking (and freezes on the final elapsed time) once `runningSince`
 * is paired with `until`.
 */
export function ElapsedTimer({
  runningSince,
  until,
}: {
  runningSince: number;
  until: number | null;
}) {
  const [now, setNow] = useState(() => until ?? Date.now());

  useEffect(() => {
    if (until !== null) return;
    const interval = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(interval);
  }, [until]);

  const elapsedSeconds = Math.max(0, Math.round(((until ?? now) - runningSince) / 1000));
  const label =
    elapsedSeconds < 60
      ? `${elapsedSeconds}s`
      : `${Math.floor(elapsedSeconds / 60)}m ${elapsedSeconds % 60}s`;

  return <span className="font-technical tabular-nums">{label}</span>;
}
