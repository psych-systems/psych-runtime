"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { useSession } from "@/components/auth/session-provider";
import { API_BASE_URL, getConfig, getHealth } from "@/lib/api";
import { describeApiError } from "@/lib/errors";
import type { ConfigResponse } from "@/lib/types";

const POLL_INTERVAL_MS = 15_000;

export type BackendStatus = "checking" | "online" | "offline";

/**
 * Is the backend up, and what is this account's console configured with.
 *
 * Two questions, two endpoints, and keeping them apart matters.
 *
 * Liveness comes from `GET /api/health`, which answers without a session.
 * This used to poll `GET /api/config` for both, which stopped working the
 * moment that route became per-account: an anonymous visitor got a 401 every
 * fifteen seconds, forever, and the health indicator read "offline" about a
 * backend that was answering perfectly well.
 *
 * The configuration itself is only fetched while signed in, because there is
 * no such thing as the configuration when nobody is asking.
 *
 * One poll loop for the whole app rather than one per consumer, via the
 * provider in `src/components/app-shell/backend-config-provider.tsx`.
 */
export function useBackendConfigPoll() {
  const [config, setConfig] = useState<ConfigResponse | null>(null);
  const [status, setStatus] = useState<BackendStatus>("checking");
  const [error, setError] = useState<string | null>(null);
  const mountedRef = useRef(true);
  const { status: session } = useSession();
  const signedIn = session === "signed-in";

  const refresh = useCallback(async () => {
    try {
      await getHealth();
      if (!mountedRef.current) return;
      setStatus("online");
      setError(null);
    } catch (err) {
      if (!mountedRef.current) return;
      setConfig(null);
      setStatus("offline");
      setError(describeApiError(err));
      return;
    }
    if (!signedIn) {
      setConfig(null);
      return;
    }
    try {
      const cfg = await getConfig();
      if (mountedRef.current) setConfig(cfg);
    } catch (err) {
      // The backend answered health, so it is up; this is a session that has
      // just expired or a configuration read that failed on its own. Leaving
      // the indicator green and the config empty is the truthful pair, and
      // `SessionProvider` is what handles an expired session.
      if (mountedRef.current) setConfig(null);
      void err;
    }
  }, [signedIn]);

  useEffect(() => {
    mountedRef.current = true;
    // Deferred: see use-agents.ts for why this isn't called inline.
    const id = setTimeout(() => void refresh(), 0);
    const timer = setInterval(() => void refresh(), POLL_INTERVAL_MS);
    return () => {
      mountedRef.current = false;
      clearTimeout(id);
      clearInterval(timer);
    };
  }, [refresh]);

  return { config, status, error, refresh, baseUrl: API_BASE_URL };
}
