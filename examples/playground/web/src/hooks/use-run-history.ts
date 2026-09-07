"use client";

import { useCallback, useEffect, useState } from "react";

import { listRuns } from "@/lib/api";
import { describeApiError } from "@/lib/errors";
import type { RunSummary } from "@/lib/types";

const ACTIVE_POLL_MS = 4_000;

/**
 * `GET /api/runs`, newest first, polled while any run is still active so the
 * history panel notices a run settling without a manual refresh.
 *
 * `watchRunId` is the conversation currently on screen. Without it the panel
 * showed "Nothing here yet" beside a conversation the person was actively
 * having: the list is fetched once on mount, and the poll only arms when a run
 * *already in the list* is active, so the very first dispatch satisfied
 * neither condition and nothing ever asked again. Passing the open run closes
 * that hole at its source rather than making every caller remember to refresh
 * after dispatching.
 */
export function useRunHistory(watchRunId: string | null = null) {
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const list = await listRuns();
      setRuns([...list].sort((a, b) => b.started_at.localeCompare(a.started_at)));
      setError(null);
    } catch (err) {
      setError(describeApiError(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // See use-agents.ts: deferred so this effect doesn't call a memoized,
    // state-setting callback directly in its own synchronous body.
    const id = setTimeout(() => void refresh(), 0);
    return () => clearTimeout(id);
  }, [refresh]);

  // A run the caller is watching that the list has never heard of is a
  // conversation just dispatched. Fetch once, immediately, rather than waiting
  // for a poll that is not running yet.
  const known = runs !== null && watchRunId !== null
    ? runs.some((run) => run.run_id === watchRunId)
    : true;
  useEffect(() => {
    if (known) return;
    const id = setTimeout(() => void refresh(), 0);
    return () => clearTimeout(id);
  }, [known, refresh]);

  useEffect(() => {
    const hasActive = (runs ?? []).some(
      (run) => run.state === "running" || run.state === "suspended" || run.state === "runnable"
    );
    // Also poll while the watched conversation is still catching up, so its
    // row appears and then settles without anyone touching anything.
    if (!hasActive && known) return;
    const timer = setInterval(() => void refresh(), ACTIVE_POLL_MS);
    return () => clearInterval(timer);
  }, [runs, known, refresh]);

  return { runs, loading, error, refresh };
}
