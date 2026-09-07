"use client";

import { useCallback, useEffect, useState } from "react";

import { getThreadReport } from "@/lib/api";
import { describeApiError } from "@/lib/errors";
import type { ThreadReport } from "@/lib/types";

/**
 * A whole conversation's report, from any Run in it.
 *
 * One request, not one per message: `GET /api/threads/{id}/report` returns the
 * totals plus every Run's own report, so switching between the conversation
 * and one message costs nothing and the two views can never disagree about
 * what happened.
 *
 * `runId` may name any Run in the chain. The backend resolves the newest and
 * walks back from there, so a link from the first message and a link from the
 * last land on the same page.
 */
export function useThreadReport(runId: string) {
  const [thread, setThread] = useState<ThreadReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setThread(await getThreadReport(runId));
    } catch (err) {
      setError(describeApiError(err));
    } finally {
      setLoading(false);
    }
  }, [runId]);

  useEffect(() => {
    // Deferred a tick: `load` sets state before its first await, and calling
    // it inline would commit a second update in the pass this mounts in.
    const id = setTimeout(() => void load(), 0);
    return () => clearTimeout(id);
  }, [load]);

  return { thread, loading, error, refresh: load };
}
