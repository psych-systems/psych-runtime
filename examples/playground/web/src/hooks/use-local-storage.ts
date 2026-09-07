"use client";

import { useCallback, useMemo, useSyncExternalStore } from "react";

/**
 * A `useState` that persists to `localStorage` and stays consistent across
 * every hook instance watching the same key in this tab (`setValue` here,
 * or a `storage` event from another tab).
 *
 * Built on `useSyncExternalStore` rather than "read in an effect, setState
 * once mounted" -- the effect-based version has to render `initialValue`
 * first and swap in the stored value a tick later (a hydration-safe render,
 * but a visible flicker for anything the user would notice changing).
 * `useSyncExternalStore`'s client/server snapshot split does the same
 * hydration-safety without that extra render: `initialValue` is used only
 * for `getServerSnapshot` (matching what the server rendered), and the real
 * client snapshot -- read directly, not queued into an effect -- is what
 * paints as soon as this hook first runs in the browser.
 */
export function useLocalStorage<T>(
  key: string,
  initialValue: T
): readonly [T, (next: T | ((prev: T) => T)) => void] {
  const subscribe = useCallback(
    (onChange: () => void) => {
      const listeners = listenersFor(key);
      listeners.add(onChange);
      const onStorage = (event: StorageEvent) => {
        if (event.key === key) onChange();
      };
      window.addEventListener("storage", onStorage);
      return () => {
        listeners.delete(onChange);
        window.removeEventListener("storage", onStorage);
      };
    },
    [key]
  );

  const getSnapshot = useCallback(() => readCached<T>(key, initialValue), [key, initialValue]);
  const getServerSnapshot = useCallback(() => initialValue, [initialValue]);

  const value = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);

  const setValue = useCallback(
    (next: T | ((prev: T) => T)) => {
      const prev = readCached<T>(key, initialValue);
      const resolved = typeof next === "function" ? (next as (p: T) => T)(prev) : next;
      try {
        window.localStorage.setItem(key, JSON.stringify(resolved));
      } catch {
        // Storage full, disabled, or unavailable (private browsing). The
        // write is best-effort; other tabs and future loads simply won't
        // see it.
      }
      cacheFor(key).raw = undefined; // force the next read to re-parse
      for (const listener of listenersFor(key)) listener();
    },
    [key, initialValue]
  );

  return useMemo(() => [value, setValue] as const, [value, setValue]);
}

interface Cache<T> {
  raw: string | null | undefined;
  value: T;
}

const caches = new Map<string, Cache<unknown>>();
const listeners = new Map<string, Set<() => void>>();

function cacheFor<T>(key: string): Cache<T> {
  let entry = caches.get(key) as Cache<T> | undefined;
  if (!entry) {
    entry = { raw: undefined, value: undefined as T };
    caches.set(key, entry as Cache<unknown>);
  }
  return entry;
}

function listenersFor(key: string): Set<() => void> {
  let set = listeners.get(key);
  if (!set) {
    set = new Set();
    listeners.set(key, set);
  }
  return set;
}

/** Reads `key` from `localStorage`, parsing lazily and only when the raw
 * string has changed since the last read -- `useSyncExternalStore` requires
 * `getSnapshot` to return a stable reference when nothing changed, and
 * `JSON.parse` on every call would break that (an infinite render loop). */
function readCached<T>(key: string, fallback: T): T {
  let raw: string | null;
  try {
    raw = window.localStorage.getItem(key);
  } catch {
    return fallback;
  }
  const entry = cacheFor<T>(key);
  if (entry.raw === raw) return entry.value;

  let value = fallback;
  if (raw !== null) {
    try {
      value = JSON.parse(raw) as T;
    } catch {
      value = fallback;
    }
  }
  entry.raw = raw;
  entry.value = value;
  return value;
}
