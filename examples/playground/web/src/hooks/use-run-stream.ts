"use client";

import { useCallback, useEffect, useState } from "react";

import { getRunStatus, streamRun } from "@/lib/api";
import { describeApiError } from "@/lib/errors";
import type { Lifecycle } from "@/components/ui/status";
import type { PsychRecord } from "@/lib/types";

export type StreamStatus = "idle" | "connecting" | "live" | "reconnecting" | "closed" | "error";

const RECONNECT_BASE_DELAY_MS = 1_000;
const IDLE_REATTACH_DELAY_MS = 750;
const MAX_RECONNECT_ATTEMPTS = 6;

/** The lifecycles from which no further Record will ever arrive. Everything
 * else, `stopping` included, still has a settlement to write. */
const FINISHED: ReadonlySet<Lifecycle> = new Set<Lifecycle>(["done", "failed", "stopped"]);

/**
 * Attaches to `GET /api/runs/{id}/stream` and keeps a Run's Records current.
 *
 * Two different things end a stream, and this hook has to tell them apart.
 * A dropped connection throws, and is retried with backoff. A clean `event:
 * done` does *not* mean the Run is over: the backend also sends it when the
 * stream has been idle, so treating it as terminal is how a long tool call or
 * a slow model left the transcript frozen with a spinner that never resolved.
 * On `done` this asks the Run itself -- `psych.status()`, the same fold a
 * Worker uses -- and only stops when the Run is genuinely finished. Otherwise
 * it re-attaches with `after=<highest seq seen>`, so nothing already rendered
 * arrives twice.
 *
 * The `after` bookkeeping is local rather than `EventSource`'s, which always
 * replays from the URL it was constructed with and would duplicate the whole
 * log on every reconnect.
 */
export function useRunStream(runId: string | null) {
  const [records, setRecords] = useState<PsychRecord[]>([]);
  const [status, setStatus] = useState<StreamStatus>("idle");
  const [error, setError] = useState<string | null>(null);

  // Bumping this re-runs the effect and re-attaches even when `runId` has not
  // changed, which is what a manual "reconnect" button needs.
  const [retryToken, setRetryToken] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;
    let lastSeq = 0;
    const seen = new Set<number>();

    const sleep = (ms: number) =>
      new Promise<void>((resolve) => setTimeout(resolve, ms));

    /** True when the Run can still write Records. A status read that fails
     * answers "keep listening": a UI that gives up on a transient 502 is
     * worse than one that reconnects once too often. */
    async function stillRunning(id: string): Promise<boolean> {
      try {
        const state = await getRunStatus(id, controller.signal);
        return !FINISHED.has(state.lifecycle);
      } catch {
        return !cancelled;
      }
    }

    async function attach(id: string) {
      let attempts = 0;
      while (!cancelled) {
        setStatus(attempts === 0 && lastSeq === 0 ? "connecting" : "reconnecting");
        try {
          for await (const record of streamRun(id, { after: lastSeq, signal: controller.signal })) {
            if (cancelled) return;
            setStatus("live");
            setError(null);
            attempts = 0;
            if (seen.has(record.seq)) continue;
            seen.add(record.seq);
            lastSeq = Math.max(lastSeq, record.seq);
            setRecords((prev) => [...prev, record].sort((a, b) => a.seq - b.seq));
          }
          if (cancelled) return;
          if (!(await stillRunning(id))) {
            setStatus("closed");
            return;
          }
          // An idle timeout, not an ending. Re-attach where we left off.
          setStatus("reconnecting");
          await sleep(IDLE_REATTACH_DELAY_MS);
        } catch (err) {
          if (cancelled || (err instanceof DOMException && err.name === "AbortError")) return;
          attempts += 1;
          setError(describeApiError(err));
          if (attempts > MAX_RECONNECT_ATTEMPTS) {
            setStatus("error");
            return;
          }
          setStatus("reconnecting");
          await sleep(RECONNECT_BASE_DELAY_MS * attempts);
        }
      }
    }

    // Deferred a tick: clearing the records and kicking off `attach` are this
    // effect reacting to `runId`, not work that belongs in the commit that
    // changed it. See use-agents.ts for the same reasoning.
    const kickoffId = setTimeout(() => {
      setRecords([]);
      setError(null);
      if (runId === null) {
        setStatus("idle");
        return;
      }
      void attach(runId);
    }, 0);

    return () => {
      cancelled = true;
      controller.abort();
      clearTimeout(kickoffId);
    };
  }, [runId, retryToken]);

  const retry = useCallback(() => setRetryToken((n) => n + 1), []);

  return { records, status, error, retry };
}
