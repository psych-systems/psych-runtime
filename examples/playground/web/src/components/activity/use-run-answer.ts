"use client";

import { useEffect, useState } from "react";

import { getRunAnswer } from "@/lib/api";
import { describeApiError } from "@/lib/errors";
import type { AnswerView } from "@/lib/types";

/**
 * `GET /api/runs/{id}/answer`: the run split into what it concluded and the
 * work behind it.
 *
 * Activity used to reconstruct the answer itself, by walking the thread for
 * the last assistant message with text in it. That is a guess, and it is the
 * wrong guess whenever the run ended on a tool result or was interrupted: the
 * split here is derived from the log by the same projection Chat renders, so
 * the two surfaces cannot disagree about what a run concluded.
 */
export function useRunAnswer(runId: string) {
  const [answer, setAnswer] = useState<AnswerView | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // A changed run id means the answer in state belongs to another run.
  // Cleared during render rather than in the effect below, so this page never
  // shows one run's answer under another run's name.
  const [prevRunId, setPrevRunId] = useState(runId);
  if (runId !== prevRunId) {
    setPrevRunId(runId);
    setAnswer(null);
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
          const next = await getRunAnswer(runId, controller.signal);
          if (controller.signal.aborted) return;
          setAnswer(next);
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

  return { answer, loading, error };
}
