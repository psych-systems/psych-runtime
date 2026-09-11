"use client";

import { useEffect, useMemo, useState } from "react";

import { getRunStatus } from "@/lib/api";
import type { RunStatus } from "@/lib/types";

const CONCURRENCY = 6;

/**
 * `GET /api/runs/{id}/status` for a set of Runs at once.
 *
 * `GET /api/runs` carries no counts and no failure text, so a list built only
 * from it can say a conversation failed but never why, which is the one thing
 * an operator opened Conversations to find out. The status endpoint is the fold the
 * runtime itself uses, so the counts here and the counts on the run detail
 * cannot disagree.
 *
 * A status that fails to load is simply absent from the map. One unreachable
 * Run must not blank the whole list: the rest of the page is still true.
 */
export function useRunStatuses(
  runIds: readonly string[],
  /** Anything that changing means the statuses are stale. The list passes the
   *  Runs' own coarse states, so a Run the list poll has seen settle is
   *  re-read here instead of keeping a "Working" pill forever. */
  revalidateOn = ""
): {
  statuses: Map<string, RunStatus>;
  loading: boolean;
} {
  // Keyed on the joined ids rather than the array: the caller rebuilds the
  // array on every render, and depending on the array itself would refetch
  // every status on every render.
  const key = runIds.join(",");
  const ids = useMemo(() => (key.length === 0 ? [] : key.split(",")), [key]);

  const [statuses, setStatuses] = useState<Map<string, RunStatus>>(new Map());
  const [loading, setLoading] = useState(ids.length > 0);

  // Adjusted during render rather than in an effect. Statuses for Runs still
  // in the list are kept while the re-read is in flight: dropping them would
  // blank every count on the page each time the list polls, which reads as
  // the numbers being unreliable rather than merely a moment old.
  const [prevKey, setPrevKey] = useState(key);
  if (key !== prevKey) {
    setPrevKey(key);
    setStatuses((current) => {
      const kept = new Map<string, RunStatus>();
      for (const id of ids) {
        const status = current.get(id);
        if (status !== undefined) kept.set(id, status);
      }
      return kept;
    });
    setLoading(ids.length > 0);
  }

  useEffect(() => {
    if (ids.length === 0) return;
    const controller = new AbortController();

    const timer = setTimeout(() => {
      void (async () => {
        const found = new Map<string, RunStatus>();
        let next = 0;
        const worker = async (): Promise<void> => {
          while (next < ids.length) {
            const id = ids[next++];
            try {
              const status = await getRunStatus(id, controller.signal);
              found.set(id, status);
            } catch {
              // Left out of the map. The row falls back to the list's own
              // coarse state rather than showing nothing at all.
            }
            if (controller.signal.aborted) return;
            // Published as they land so a long list fills in progressively
            // instead of staying blank until the slowest read returns.
            setStatuses((current) => new Map([...current, ...found]));
          }
        };
        await Promise.all(
          Array.from({ length: Math.min(CONCURRENCY, ids.length) }, () => worker())
        );
        if (!controller.signal.aborted) setLoading(false);
      })();
    }, 0);

    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [ids, revalidateOn]);

  return { statuses, loading };
}
