"use client";

import { useEffect, useState } from "react";

import { getRunThread } from "@/lib/api";
import { describeApiError } from "@/lib/errors";
import type { RunThread } from "@/lib/types";

/**
 * `GET /api/runs/{id}/thread`: the whole conversation this Run belongs to.
 *
 * The run detail leads with what the person asked and what the agent answered,
 * and neither is on the status or the report in a form worth reading. The
 * thread is, and it also names every Run in the chain, which is what the turn
 * switcher on this page is built from.
 */
export function useRunThread(runId: string) {
  const [thread, setThread] = useState<RunThread | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // A Run whose id changed has a stale thread until the fetch below lands.
  // Cleared during render rather than in the effect, so this page never shows
  // the previous conversation's messages under this conversation's name.
  const [prevRunId, setPrevRunId] = useState(runId);
  if (runId !== prevRunId) {
    setPrevRunId(runId);
    setThread(null);
    setError(null);
    setLoading(true);
  }

  useEffect(() => {
    const controller = new AbortController();
    // Deferred a tick so this effect does not commit a state update in the
    // pass it mounts in. Same shape as the other loaders in this app.
    const timer = setTimeout(() => {
      void (async () => {
        try {
          const next = await getRunThread(runId, controller.signal);
          if (controller.signal.aborted) return;
          setThread(next);
          setError(null);
        } catch (err) {
          if (controller.signal.aborted) return;
          setError(describeApiError(err));
        } finally {
          if (!controller.signal.aborted) setLoading(false);
        }
      })();
    }, 0);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [runId]);

  return { thread, loading, error };
}
