"use client";

import { useCallback, useEffect, useState } from "react";

import { getRunStatus } from "@/lib/api";
import { describeApiError } from "@/lib/errors";
import type { Lifecycle } from "@/components/ui/status";
import type { RunStatus } from "@/lib/types";

const ACTIVE: ReadonlySet<Lifecycle> = new Set<Lifecycle>([
  "queued",
  "running",
  "waiting",
  "stopping",
]);

const POLL_INTERVAL_MS = 3_000;

/**
 * What the Run is doing right now, from `GET /api/runs/{id}/status`.
 *
 * The record stream says what has happened; this says what is true. They are
 * not the same question, and two of the answers a chat surface needs are only
 * on this one: the exact call a waiting Run wants approved, and `stopping` --
 * an abort that has landed in the log with no terminal record yet. A UI
 * without that word shows "Working" and offers a stop button that does
 * nothing, which is how a person concludes stop is broken.
 *
 * `refreshKey` re-reads on demand (the caller passes the stream's record
 * count, so a Run that just did something is re-read immediately); the poll
 * is the floor under it, for the states that change with no record at all.
 */
export function useRunStatus(runId: string | null, refreshKey = 0) {
  const [status, setStatus] = useState<RunStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(runId !== null);
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    const controller = new AbortController();

    // Deferred a tick: this effect reacts to `runId` changing rather than
    // needing its state updates in the commit that changed it. See
    // use-agents.ts for the same reasoning.
    const kickoffId = setTimeout(() => {
      if (runId === null) {
        setStatus(null);
        setError(null);
        setLoading(false);
        return;
      }
      void (async () => {
        try {
          const next = await getRunStatus(runId, controller.signal);
          setStatus(next);
          setError(null);
        } catch (err) {
          if (err instanceof DOMException && err.name === "AbortError") return;
          setError(describeApiError(err));
        } finally {
          if (!controller.signal.aborted) setLoading(false);
        }
      })();
    }, 0);

    return () => {
      controller.abort();
      clearTimeout(kickoffId);
    };
  }, [runId, refreshKey, reloadToken]);

  // A Run whose id changed has a stale status until the fetch above lands.
  // Cleared here rather than in the effect so the header never shows the
  // previous conversation's state against this conversation's name.
  const [prevRunId, setPrevRunId] = useState(runId);
  if (runId !== prevRunId) {
    setPrevRunId(runId);
    setStatus(null);
    setError(null);
    setLoading(runId !== null);
  }

  const active = status !== null && ACTIVE.has(status.lifecycle);
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setReloadToken((n) => n + 1), POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [active]);

  const refresh = useCallback(() => setReloadToken((n) => n + 1), []);

  return { status, error, loading, refresh };
}
