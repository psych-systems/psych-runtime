"use client";

import { useCallback, useEffect, useState } from "react";

import { listAgents } from "@/lib/api";
import { describeApiError } from "@/lib/errors";
import type { AgentSummary } from "@/lib/types";

export function useAgents() {
  const [agents, setAgents] = useState<AgentSummary[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const list = await listAgents();
      setAgents(list);
      setError(null);
    } catch (err) {
      setAgents(null);
      setError(describeApiError(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // Deferred a tick rather than called directly: `refresh` sets `loading`
    // synchronously before its first `await`, and running that inline here
    // would mean this effect commits a second state update in the same
    // pass it mounts in.
    const id = setTimeout(() => void refresh(), 0);
    return () => clearTimeout(id);
  }, [refresh]);

  return { agents, loading, error, refresh };
}
