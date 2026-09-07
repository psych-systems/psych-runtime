"use client";

import { createContext, useContext, type ReactNode } from "react";

import { useBackendConfigPoll, type BackendStatus } from "@/hooks/use-backend-config";
import type { ConfigResponse } from "@/lib/types";

interface BackendConfigContextValue {
  config: ConfigResponse | null;
  status: BackendStatus;
  error: string | null;
  refresh: () => void;
  baseUrl: string;
}

const BackendConfigContext = createContext<BackendConfigContextValue | null>(null);

/** One `/api/config` poll loop for the whole app, shared by the health
 * indicator, the agent picker and the composer -- each of which cares
 * whether the backend is reachable right now, but none of which should run
 * its own timer. */
export function BackendConfigProvider({ children }: { children: ReactNode }) {
  const value = useBackendConfigPoll();
  return (
    <BackendConfigContext.Provider value={value}>{children}</BackendConfigContext.Provider>
  );
}

export function useBackendConfig(): BackendConfigContextValue {
  const ctx = useContext(BackendConfigContext);
  if (ctx === null) {
    throw new Error("useBackendConfig must be used within a BackendConfigProvider");
  }
  return ctx;
}
