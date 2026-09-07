"use client";

import { useSyncExternalStore } from "react";

const subscribe = () => () => {};

/**
 * `true` once this render is happening in the browser, `false` during SSR
 * and the first hydration pass -- for the handful of things (the resolved
 * theme icon, anything reading `window`) that must match the server on
 * first paint and only diverge after. `useSyncExternalStore`'s
 * server/client snapshot split gives this without an effect + `setState`
 * flipping it a tick after mount.
 */
export function useIsClient(): boolean {
  return useSyncExternalStore(
    subscribe,
    () => true,
    () => false
  );
}
