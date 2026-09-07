"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import { getRunThread, streamRun } from "@/lib/api";
import { buildConversation, type Conversation, type ThreadRun } from "@/lib/conversation";
import { describeApiError } from "@/lib/errors";
import { useRunStream } from "@/hooks/use-run-stream";
import type { PsychRecord } from "@/lib/types";

/** Reads one Run's whole log and returns when the server closes the stream.
 * Every Run but the newest in a chain has settled, so this is a bounded
 * replay rather than a subscription. Records rather than `/messages`: the
 * flat projection has no tool arguments, results or approvals, and an
 * earlier turn deserves the same reading as the newest one. */
async function readLog(runId: string, signal: AbortSignal): Promise<PsychRecord[]> {
  const records: PsychRecord[] = [];
  for await (const record of streamRun(runId, { after: 0, signal })) records.push(record);
  return records;
}

const EMPTY_LOGS: ReadonlyMap<string, PsychRecord[]> = new Map();

interface UseConversationResult {
  conversation: Conversation;
  /** Earlier turns are still being read. The newest Run renders immediately
   * either way, so this never gates the live stream. */
  loadingHistory: boolean;
  historyError: string | null;
  stream: ReturnType<typeof useRunStream>;
}

/**
 * The whole conversation a Run belongs to: every earlier Run's log, read
 * once, plus the newest Run's live stream.
 *
 * `continuesFrom` is what the caller just dispatched against. Given it, a new
 * turn extends the chain already on screen instead of blanking it while
 * `GET /api/runs/{id}/thread` confirms what we already know, which is the
 * whole conversation flickering away the moment you press send.
 */
export function useConversation(
  runId: string | null,
  continuesFrom: string | null
): UseConversationResult {
  const stream = useRunStream(runId);

  // Logs are keyed by Run and kept across navigations: a settled Run's log is
  // immutable, so re-reading it when someone clicks back is pure waste. The
  // ref is the copy the effect consults; the state is the copy the render
  // reads, and they are replaced together.
  const cacheRef = useRef<ReadonlyMap<string, PsychRecord[]>>(EMPTY_LOGS);
  const [logs, setLogs] = useState<ReadonlyMap<string, PsychRecord[]>>(EMPTY_LOGS);
  const [chain, setChain] = useState<string[]>(runId === null ? [] : [runId]);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);

  // Adjusted during render rather than in an effect: the chain the previous
  // Run left behind must not be rendered against this Run for even one frame.
  // https://react.dev/learn/you-might-not-need-an-effect
  const [prevRunId, setPrevRunId] = useState(runId);
  if (runId !== prevRunId) {
    setPrevRunId(runId);
    setHistoryError(null);
    if (runId === null) {
      setChain([]);
    } else if (continuesFrom !== null && chain[chain.length - 1] === continuesFrom) {
      setChain([...chain, runId]);
    } else {
      setChain([runId]);
    }
  }

  useEffect(() => {
    if (runId === null) return;
    const controller = new AbortController();
    let cancelled = false;

    // Deferred a tick, as everywhere else in this codebase: this is the
    // effect reacting to `runId`, not work owed to the commit that set it.
    const kickoffId = setTimeout(() => {
      setLoadingHistory(true);
      void (async () => {
        try {
          const thread = await getRunThread(runId, controller.signal);
          if (cancelled) return;
          // `run_ids` is oldest first and ends at this Run. Trust it over the
          // optimistic chain above, which only guessed to avoid a flicker.
          const ids = thread.run_ids.length > 0 ? thread.run_ids : [runId];
          setChain(ids);

          const missing = ids.filter((id) => id !== runId && !cacheRef.current.has(id));
          const fetched = await Promise.all(
            missing.map(async (id) => [id, await readLog(id, controller.signal)] as const)
          );
          if (cancelled || fetched.length === 0) return;
          const next = new Map(cacheRef.current);
          for (const [id, records] of fetched) next.set(id, records);
          cacheRef.current = next;
          setLogs(next);
        } catch (err) {
          if (cancelled || (err instanceof DOMException && err.name === "AbortError")) return;
          setHistoryError(describeApiError(err));
        } finally {
          if (!cancelled) setLoadingHistory(false);
        }
      })();
    }, 0);

    return () => {
      cancelled = true;
      controller.abort();
      clearTimeout(kickoffId);
    };
  }, [runId]);

  const conversation = useMemo(() => {
    const runs: ThreadRun[] = chain.map((id) => ({
      runId: id,
      records: id === runId ? stream.records : (logs.get(id) ?? []),
    }));
    return buildConversation(runs);
  }, [chain, runId, stream.records, logs]);

  return { conversation, loadingHistory, historyError, stream };
}
