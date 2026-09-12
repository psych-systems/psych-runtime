"use client";

import type {
  LocalPeerRequest,
  ModelPrice,
  RuntimeSettingsIn,
  SandboxProfileHealth,
  SkillIn,
} from "@/lib/types";
import type { A2APeerPreset } from "@/components/settings/types";
import { useCallback, useEffect, useState } from "react";

import {
  activateProvider,
  addLocalPeer,
  checkSandboxProfile,
  deleteSecret,
  updateRuntimeSettings,
  getSettings,
  testProvider,
  updateMcpSettings,
  updateProviders,
  updateModelPrices,
  updateSkills,
  updateA2APeers,
  updateSecrets,
} from "@/lib/api";
import { ApiError } from "@/lib/api";
import { describeApiError } from "@/lib/errors";
import type {
  McpServerPresetIn,
  PlaygroundSettings,
  ProviderIn,
  ProviderOut,
  ProviderTestResult,
} from "@/components/settings/types";
import { providerOutToIn } from "@/components/settings/types";

/**
 * Loads the playground's settings and exposes every mutation over them.
 *
 * `updateProviders`/`updateMcpSettings` are full-collection PUTs on the
 * wire (`app.settings_store`'s own docstring: "a full replace of the
 * provider list"), so every write here resends the whole array -- existing
 * entries converted back with `providerOutToIn` so an edit to one provider
 * never touches another's stored key. Secrets are the opposite: a value is
 * write-only and never read back, so there is no array to resend without
 * destroying every secret this dialog didn't just set. `saveSecret` upserts
 * one name at a time against `PUT /api/settings/secrets`, which merges
 * (`EnvSecretResolver.set_secret`) rather than replacing.
 */
export function useSettings() {
  const [settings, setSettings] = useState<PlaygroundSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const data = await getSettings();
      setSettings(data);
      setError(null);
    } catch (err) {
      setSettings(null);
      setError(describeApiError(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // Deferred a tick rather than called directly: `refresh` sets `loading`
    // synchronously before its first `await`, and running that inline here
    // would mean this effect commits a second state update in the same pass
    // it mounts in. Matches `useAgents`' own fix for the identical shape.
    const id = setTimeout(() => void refresh(), 0);
    return () => clearTimeout(id);
  }, [refresh]);

  const saveProvider = useCallback(
    async (edited: ProviderIn) => {
      const current = settings?.providers ?? [];
      const others = current.filter((p) => p.id !== edited.id).map(providerOutToIn);
      await updateProviders([...others, edited]);
      await refresh();
    },
    [settings, refresh]
  );

  const removeProvider = useCallback(
    async (id: string) => {
      // Deleting one provider is writing back the others, since the endpoint
      // replaces the whole list. Converting the survivors with
      // `providerOutToIn` is what keeps their stored keys: an entry sent
      // without `api_key` keeps whatever is already held for it.
      const others = (settings?.providers ?? [])
        .filter((provider) => provider.id !== id)
        .map(providerOutToIn);
      await updateProviders(others);
      await refresh();
    },
    [settings, refresh]
  );

  const activate = useCallback(
    async (id: string) => {
      await activateProvider(id);
      await refresh();
    },
    [refresh]
  );

  const runTest = useCallback(async (id: string): Promise<ProviderTestResult> => {
    try {
      return await testProvider(id);
    } catch (err) {
      // A non-2xx from the test endpoint itself (the id doesn't exist, the
      // server errored before it could even try the provider) still needs
      // to render as a result rather than throwing through the dialog.
      if (err instanceof ApiError) {
        return { ok: false, detail: err.message };
      }
      return { ok: false, detail: describeApiError(err) };
    }
  }, []);

  const savePreset = useCallback(
    async (edited: McpServerPresetIn, previousName: string | null) => {
      const current = settings?.mcp_servers ?? [];
      const others = current.filter((p) => p.name !== (previousName ?? edited.name));
      await updateMcpSettings([...others, edited]);
      await refresh();
    },
    [settings, refresh]
  );

  const removePreset = useCallback(
    async (name: string) => {
      // The endpoint replaces the whole list, so removing one connection is
      // writing back the others. A connection here is only ever the starting
      // point for building an agent, so removing it cannot affect an agent
      // already published against it: that one carries its own copy.
      const others = (settings?.mcp_servers ?? []).filter((p) => p.name !== name);
      await updateMcpSettings(others);
      await refresh();
    },
    [settings, refresh]
  );

  const saveSecret = useCallback(
    async (name: string, value: string) => {
      await updateSecrets({ [name]: value });
      await refresh();
    },
    [refresh]
  );

  const removeSecret = useCallback(
    async (name: string) => {
      await deleteSecret(name);
      await refresh();
    },
    [refresh]
  );

  const savePeers = useCallback(
    async (peers: A2APeerPreset[]) => {
      await updateA2APeers(peers);
      await refresh();
    },
    [refresh]
  );

  const saveSkills = useCallback(
    async (skills: SkillIn[]) => {
      await updateSkills(skills);
      await refresh();
    },
    [refresh]
  );

  const saveRuntime = useCallback(
    async (runtime: RuntimeSettingsIn) => {
      await updateRuntimeSettings(runtime);
      await refresh();
    },
    [refresh]
  );

  const checkSandbox = useCallback(
    (name: string): Promise<SandboxProfileHealth> => checkSandboxProfile(name),
    []
  );

  const addOwnAgentAsPeer = useCallback(
    async (body: LocalPeerRequest) => {
      await addLocalPeer(body);
      await refresh();
    },
    [refresh]
  );

  const savePrices = useCallback(
    async (prices: ModelPrice[]) => {
      await updateModelPrices(prices);
      await refresh();
    },
    [refresh]
  );

  return {
    settings,
    loading,
    error,
    refresh,
    saveProvider,
    removeProvider,
    activate,
    runTest,
    savePreset,
    removePreset,
    saveSecret,
    removeSecret,
    savePrices,
    saveSkills,
    savePeers,
    saveRuntime,
    checkSandbox,
    addOwnAgentAsPeer,
  };
}

export type { ProviderOut };
