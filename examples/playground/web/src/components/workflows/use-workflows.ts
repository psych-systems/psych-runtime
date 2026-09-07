"use client";

import { useCallback, useEffect, useState } from "react";

import { listWorkflows } from "@/lib/api";
import { describeApiError } from "@/lib/errors";
import type { WorkflowSummary } from "@/lib/types";

export function useWorkflows() {
  const [workflows, setWorkflows] = useState<WorkflowSummary[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setWorkflows(await listWorkflows());
      setError(null);
    } catch (err) {
      setWorkflows(null);
      setError(describeApiError(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const id = setTimeout(() => void refresh(), 0);
    return () => clearTimeout(id);
  }, [refresh]);

  return { workflows, loading, error, refresh };
}
