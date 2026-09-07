"use client";

import { useCallback, useEffect, useState } from "react";

import { getRunReport } from "@/lib/api";
import { describeApiError } from "@/lib/errors";
import type { RunReport } from "@/lib/types";

/**
 * `GET /api/runs/{id}/report`, fetched once per `runId` rather than kept
 * live. The report is a projection over the log at the moment it was read
 * (see `psych_runtime/report/model.py`); the trace view re-reads it on demand
 * (`refresh`) instead of guessing at what changed.
 */
export function useRunReport(runId: string) {
  const [report, setReport] = useState<RunReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(
    async (signal?: AbortSignal) => {
      setLoading(true);
      try {
        const next = await getRunReport(runId, signal);
        if (signal?.aborted) return;
        setReport(next);
        setError(null);
      } catch (err) {
        if (signal?.aborted) return;
        setError(describeApiError(err));
      } finally {
        if (!signal?.aborted) setLoading(false);
      }
    },
    [runId]
  );

  useEffect(() => {
    const controller = new AbortController();
    // Deferred a tick rather than called directly: `refresh` sets `loading`
    // synchronously before its first `await`, and running that inline here
    // would mean this effect commits a second state update in the same pass
    // it mounts in. See use-agents.ts for the same pattern.
    const id = setTimeout(() => void refresh(controller.signal), 0);
    return () => {
      clearTimeout(id);
      controller.abort();
    };
  }, [refresh]);

  return { report, loading, error, refresh: () => void refresh() };
}
