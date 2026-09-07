"use client";

import { useCallback, useEffect, useState } from "react";

import { listScenarios } from "@/lib/api";
import { describeApiError } from "@/lib/errors";
import type { ScenarioSummary } from "@/lib/types";

/** `GET /api/scenarios`, fetched once on mount. Same shape as `useAgents` /
 * `useTools`: `null` while unloaded (distinct from `[]`, an empty answer),
 * `error` set alongside it when the fetch failed. */
export function useScenarios() {
  const [scenarios, setScenarios] = useState<ScenarioSummary[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const list = await listScenarios();
      setScenarios(list);
      setError(null);
    } catch (err) {
      setScenarios(null);
      setError(describeApiError(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // Deferred a tick: `refresh` sets `loading` synchronously before its
    // first `await`, and calling it directly here would mean this effect
    // commits a second state update in the same pass it mounts in -- see
    // `useAgents` for the same shape.
    const id = setTimeout(() => void refresh(), 0);
    return () => clearTimeout(id);
  }, [refresh]);

  return { scenarios, loading, error, refresh };
}
