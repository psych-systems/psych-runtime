"use client";

import { useCallback, useEffect, useState } from "react";

import { fetchSubagents } from "@/lib/api";
import { describeApiError } from "@/lib/errors";
import type { SubagentTree } from "@/lib/types";

const POLL_INTERVAL_MS = 2_000;

/**
 * The subagent tree under one run.
 *
 * Polled rather than streamed, and only while something in the tree can still
 * change. The parent's record stream already wakes the page when the parent
 * writes anything, but a child writes into its *own* log: the parent hears
 * nothing at all while a child works, so a page that reacted only to the
 * parent's stream would show a tree frozen at the moment it was spawned and
 * come alive again minutes later when the child reported back. That stretch is
 * exactly the one somebody is watching.
 *
 * The poll stops as soon as the tree says it is complete, so a settled
 * conversation costs one request rather than one every two seconds forever.
 * `refreshKey` re-reads on demand, for a caller that has just steered or
 * stopped a child and should not wait out an interval to see it.
 *
 * State updates are deferred a tick for the same reason `use-run-status.ts`
 * defers its own: this effect reacts to `runId` changing rather than needing
 * its updates committed in the render that changed it.
 */
export function useSubagents(runId: string | null, live: boolean, refreshKey = 0) {
  const [tree, setTree] = useState<SubagentTree | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);

  const refresh = useCallback(() => setReloadToken((token) => token + 1), []);

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    let cancelled = false;

    async function read(id: string): Promise<void> {
      try {
        const next = await fetchSubagents(id);
        if (cancelled) return;
        setTree(next);
        setError(null);
        // Keep polling only while an answer could still change: a complete
        // tree under a settled run never grows another branch.
        if (live || !next.complete) {
          timer = setTimeout(() => void read(id), POLL_INTERVAL_MS);
        }
      } catch (err) {
        if (cancelled) return;
        setError(describeApiError(err));
      }
    }

    const kickoff = setTimeout(() => {
      if (cancelled) return;
      if (runId === null) {
        setTree(null);
        setError(null);
        return;
      }
      void read(runId);
    }, 0);

    return () => {
      cancelled = true;
      clearTimeout(kickoff);
      if (timer !== null) clearTimeout(timer);
    };
  }, [runId, live, refreshKey, reloadToken]);

  return { tree, error, refresh };
}
