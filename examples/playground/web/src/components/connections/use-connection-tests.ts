"use client";

import { useCallback, useRef, useState } from "react";

import { disconnectMcpServer, listPendingAuthorizations, testMcpServer } from "@/lib/api";
import { describeApiError } from "@/lib/errors";
import type { McpTestResult } from "@/lib/types";

interface ConnectionTests {
  /** The connection currently being tested, if any. */
  testing: string | null;
  /** A sign-in page the connection is blocked on, while it is blocked on it. */
  authUrl: string | null;
  /** Results this page has seen, kept so a test that could not even be asked
   *  for still says so. A successful test writes the backend's own record, so
   *  the card reads that instead and survives a reload. */
  results: Record<string, McpTestResult>;
  run: (name: string, options?: { force?: boolean }) => Promise<void>;
  /** Close the connection and forget its token. The preset stays. */
  disconnect: (name: string) => Promise<void>;
  forget: (name: string) => void;
}

/**
 * Runs the real connect attempt, and picks up the sign-in URL it may block on.
 *
 * A connection using the browser sign-in flow blocks server-side inside the
 * connect attempt, waiting for a person, so its authorization URL cannot come
 * back in that call's own response. It has to be collected while the call is
 * still in flight.
 *
 * Only authorizations that began after this click count. The endpoint lists
 * every one in flight across the process, and an earlier attempt nobody
 * finished stays listed until it times out, so matching on "any pending"
 * showed a sign-in prompt during a connection that never asked for one, which
 * is exactly the confusion this banner exists to prevent.
 *
 * Lives above the cards on purpose. When this state sat inside a tab that
 * Radix unmounted, every result vanished the moment someone looked at
 * something else.
 */
export function useConnectionTests(onSettled: () => Promise<void> | void): ConnectionTests {
  const [testing, setTesting] = useState<string | null>(null);
  const [authUrl, setAuthUrl] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, McpTestResult>>({});
  const settled = useRef(onSettled);
  settled.current = onSettled;

  const run = useCallback(async (name: string, options?: { force?: boolean }) => {
    setTesting(name);
    setAuthUrl(null);

    const startedAt = Date.now();
    const poll = setInterval(() => {
      void listPendingAuthorizations()
        .then((pending) => {
          const mine = pending.find((entry) => Date.parse(entry.started_at) >= startedAt - 1000);
          if (mine) setAuthUrl(mine.authorization_url);
        })
        .catch(() => {
          // A failed poll is not worth surfacing. The connect attempt's own
          // result is the answer, and this only ever adds a shortcut to it.
        });
    }, 1500);

    try {
      const result = await testMcpServer(name, options);
      setResults((current) => ({ ...current, [name]: result }));
    } catch (err) {
      // Failing to even ask is itself an answer, and it belongs beside the
      // connection rather than in a toast that disappears before it can be
      // compared with the address that caused it.
      setResults((current) => ({
        ...current,
        [name]: { ok: false, detail: describeApiError(err) },
      }));
    } finally {
      clearInterval(poll);
      setTesting(null);
      setAuthUrl(null);
      // The attempt updated the stored record, so re-read it: that record, not
      // this component's memory, is what the card shows after a reload.
      await settled.current();
    }
  }, []);

  const disconnect = useCallback(async (name: string) => {
    setTesting(name);
    try {
      await disconnectMcpServer(name);
      // Dropped rather than replaced with a "disconnected" result: the card
      // reads the backend's stored record, which this just cleared, and a
      // leftover success here would keep claiming a live connection.
      setResults((current) => {
        const next = { ...current };
        delete next[name];
        return next;
      });
    } catch (err) {
      setResults((current) => ({
        ...current,
        [name]: { ok: false, detail: describeApiError(err) },
      }));
    } finally {
      setTesting(null);
      await settled.current();
    }
  }, []);

  const forget = useCallback((name: string) => {
    setResults((current) => {
      const next = { ...current };
      delete next[name];
      return next;
    });
  }, []);

  return { testing, authUrl, results, run, disconnect, forget };
}
