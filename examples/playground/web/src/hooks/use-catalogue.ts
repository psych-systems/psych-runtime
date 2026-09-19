"use client";

import { useCallback, useEffect, useState } from "react";

import { getCatalogue, seedCatalogue } from "@/lib/api";
import { describeApiError } from "@/lib/errors";
import type { CatalogueKind, CatalogueResponse } from "@/lib/types";

/**
 * What the console offers out of the box.
 *
 * Read on every page that shows an offer rather than a thing: the connectors
 * list, the provider table, the agent and workflow lists, and the get-started
 * card. `seed` creates whatever is missing and re-reads, so a caller never has
 * to merge the response into this state by hand.
 */
export function useCatalogue() {
  const [catalogue, setCatalogue] = useState<CatalogueResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setCatalogue(await getCatalogue());
      setError(null);
    } catch (err) {
      setCatalogue(null);
      setError(describeApiError(err));
    } finally {
      setLoading(false);
    }
  }, []);

  const seed = useCallback(
    async (kinds?: CatalogueKind[]) => {
      const result = await seedCatalogue(kinds);
      await refresh();
      return result;
    },
    [refresh],
  );

  useEffect(() => {
    // Deferred a tick, for the reason `useAgents` documents: `refresh` sets
    // `loading` before its first await, and calling it inline here would
    // commit a second state update in the pass this effect mounts in.
    const id = setTimeout(() => void refresh(), 0);
    return () => clearTimeout(id);
  }, [refresh]);

  return { catalogue, loading, error, refresh, seed };
}
