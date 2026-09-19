"use client";

import { useCallback, useEffect, useState } from "react";

import { ApiError, getWorkflowView } from "@/lib/api";
import { describeApiError } from "@/lib/errors";
import type { WorkflowView } from "@/lib/types";

const POLL_INTERVAL_MS = 1_500;

/** 409 is the backend saying "this run is not a workflow", and 404 that it
 *  has none. Neither is an error a person should be shown. */
function isNotAWorkflow(err: unknown): boolean {
  return err instanceof ApiError && (err.status === 409 || err.status === 404);
}

/**
 * One workflow run, kept current while it is still going.
 *
 * Polls rather than deriving from the record stream. The tree is a
 * projection -- which `foreach` turn a step belongs to, which branch arm was
 * taken, which attempt is pending a retry -- and rebuilding that on the client
 * from `step_started` records would be a second implementation of the
 * backend's fold, free to disagree with it. `refreshKey` lets the caller
 * re-read the moment the stream delivers a record, so the poll is the floor
 * rather than the whole mechanism.
 *
 * Stops once the run has settled: a finished workflow's tree never changes
 * again, and a page left open on one should not keep asking.
 *
 * The deferred kickoff, the `AbortController` and the id-changed reset are all
 * load-bearing, and copied from `use-run-status.ts` for exactly the reasons
 * its comments give.
 */
export function useWorkflowView(runId: string | null, refreshKey = 0) {
  const [view, setView] = useState<WorkflowView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notAWorkflow, setNotAWorkflow] = useState(false);
  const [loading, setLoading] = useState(runId !== null);
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    const controller = new AbortController();

    const kickoffId = setTimeout(() => {
      if (runId === null) {
        setView(null);
        setError(null);
        setLoading(false);
        return;
      }
      void (async () => {
        try {
          const next = await getWorkflowView(runId, controller.signal);
          setView(next);
          setError(null);
          setNotAWorkflow(false);
        } catch (err) {
          if (err instanceof DOMException && err.name === "AbortError") return;
          if (isNotAWorkflow(err)) {
            setNotAWorkflow(true);
            setError(null);
          } else {
            setError(describeApiError(err));
          }
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

  const [prevRunId, setPrevRunId] = useState(runId);
  if (runId !== prevRunId) {
    setPrevRunId(runId);
    setView(null);
    setError(null);
    setNotAWorkflow(false);
    setLoading(runId !== null);
  }

  const settled = view !== null && view.terminal_state !== null;
  const active = view !== null && !settled;
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setReloadToken((n) => n + 1), POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [active]);

  const refresh = useCallback(() => setReloadToken((n) => n + 1), []);

  return { view, error, notAWorkflow, loading, refresh };
}

/**
 * Whether this run has a workflow view at all, for the view switcher.
 *
 * Answered once per run id and remembered for the life of the tab. A run does
 * not stop being a workflow, so re-asking on every render of every run page
 * would be one wasted request per navigation, and a 409 arriving as an error
 * banner on an ordinary agent run would be worse than that.
 */
const KNOWN = new Map<string, boolean>();

export function useIsWorkflowRun(runId: string | null): boolean {
  const [is, setIs] = useState<boolean>(() => (runId ? (KNOWN.get(runId) ?? false) : false));

  // The answer for a run already known is read while rendering, so switching
  // between two runs never shows the previous one's tab for a frame.
  const [prevRunId, setPrevRunId] = useState(runId);
  if (runId !== prevRunId) {
    setPrevRunId(runId);
    setIs(runId === null ? false : (KNOWN.get(runId) ?? false));
  }

  useEffect(() => {
    if (runId === null || KNOWN.has(runId)) return;
    const controller = new AbortController();
    const id = setTimeout(() => {
      void getWorkflowView(runId, controller.signal)
        .then(() => {
          KNOWN.set(runId, true);
          setIs(true);
        })
        .catch((err: unknown) => {
          if (err instanceof DOMException && err.name === "AbortError") return;
          // Only a definite "not a workflow" is remembered. A network blip
          // must not teach this tab that a workflow run has no workflow.
          if (isNotAWorkflow(err)) KNOWN.set(runId, false);
          setIs(false);
        });
    }, 0);
    return () => {
      controller.abort();
      clearTimeout(id);
    };
  }, [runId]);

  return is;
}
