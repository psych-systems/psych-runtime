"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { streamScenarioRun } from "@/lib/api";
import { describeApiError } from "@/lib/errors";
import type { ScenarioProgressEvent, ScenarioRunResult, ScenarioSummary } from "@/lib/types";

export type ScenarioRunStatus = "idle" | "running" | "passed" | "failed" | "connection-error";

export interface ScenarioRunEntry {
  status: ScenarioRunStatus;
  progress: ScenarioProgressEvent[];
  /** Set once the stream's final frame arrives -- present for both a
   * scenario that ran and reported `passed: false` (an unavailable
   * scenario, an assertion that didn't hold, an exception the endpoint
   * turned into a result) and one that reported `passed: true`. Distinct
   * from `connectionError`, which means no result ever arrived. */
  result: ScenarioRunResult | null;
  /** Set only when the transport itself failed -- the backend was
   * unreachable, or the stream ended without ever sending a result. Never
   * set for a scenario that ran and genuinely failed; that is `result`
   * with `passed: false`, a normal outcome this hook does not editorialize
   * about. */
  connectionError: string | null;
  startedAt: number | null;
  finishedAt: number | null;
}

const IDLE_ENTRY: ScenarioRunEntry = {
  status: "idle",
  progress: [],
  result: null,
  connectionError: null,
  startedAt: null,
  finishedAt: null,
};

/**
 * Drives `POST /api/scenarios/{id}/run` for however many scenario cards are
 * on the page -- one run active for the whole page at a time, not one per
 * card. Several scenarios share real Postgres/MySQL/DynamoDB instances or
 * deliberately SIGKILL a Worker process; a concurrent second run would make
 * one scenario's fixture setup or teardown corrupt another's, so `runOne`
 * refuses to start a second run rather than merely discouraging overlap in
 * the UI (which a caller still should -- see `activeId`).
 */
export function useScenarioRunner() {
  const [entries, setEntries] = useState<Record<string, ScenarioRunEntry>>({});
  const [activeId, setActiveId] = useState<string | null>(null);
  const [runningAll, setRunningAll] = useState(false);
  const activeIdRef = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  // Abort an in-flight stream if the page unmounts mid-run rather than
  // leaking the fetch reader.
  useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  const entryFor = useCallback(
    (id: string): ScenarioRunEntry => entries[id] ?? IDLE_ENTRY,
    [entries]
  );

  const patchEntry = useCallback((id: string, patch: Partial<ScenarioRunEntry>) => {
    setEntries((prev) => ({ ...prev, [id]: { ...(prev[id] ?? IDLE_ENTRY), ...patch } }));
  }, []);

  const runOne = useCallback(
    async (scenario: ScenarioSummary): Promise<void> => {
      if (activeIdRef.current !== null) return;
      activeIdRef.current = scenario.id;
      setActiveId(scenario.id);

      const controller = new AbortController();
      abortRef.current = controller;
      setEntries((prev) => ({
        ...prev,
        [scenario.id]: { ...IDLE_ENTRY, status: "running", startedAt: Date.now() },
      }));

      let gotResult = false;
      try {
        for await (const event of streamScenarioRun(scenario.id, controller.signal)) {
          if (event.kind === "progress") {
            setEntries((prev) => {
              const cur = prev[scenario.id] ?? IDLE_ENTRY;
              const frame: ScenarioProgressEvent = { step: event.step, detail: event.detail, at: event.at };
              return { ...prev, [scenario.id]: { ...cur, progress: [...cur.progress, frame] } };
            });
          } else {
            gotResult = true;
            patchEntry(scenario.id, {
              status: event.result.passed ? "passed" : "failed",
              result: event.result,
              finishedAt: Date.now(),
            });
          }
        }
        if (!gotResult) {
          patchEntry(scenario.id, {
            status: "connection-error",
            connectionError: "the stream ended before the scenario reported a result",
            finishedAt: Date.now(),
          });
        }
      } catch (err) {
        if (!(err instanceof DOMException && err.name === "AbortError")) {
          patchEntry(scenario.id, {
            status: "connection-error",
            connectionError: describeApiError(err),
            finishedAt: Date.now(),
          });
        }
      } finally {
        abortRef.current = null;
        activeIdRef.current = null;
        setActiveId(null);
      }
    },
    [patchEntry]
  );

  const runAll = useCallback(
    async (scenarios: ScenarioSummary[]): Promise<void> => {
      if (activeIdRef.current !== null) return;
      setRunningAll(true);
      try {
        for (const scenario of scenarios) {
          if (!scenario.available) continue;
          await runOne(scenario);
        }
      } finally {
        setRunningAll(false);
      }
    },
    [runOne]
  );

  return { entryFor, activeId, runningAll, runOne, runAll };
}
