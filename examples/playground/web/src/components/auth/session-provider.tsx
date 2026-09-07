"use client";

import { usePathname, useRouter } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

import {
  BackendUnreachableError,
  NotSignedInError,
  getHealth,
  signIn as postSignIn,
  signOut as postSignOut,
  signUp as postSignUp,
  whoami,
} from "@/lib/api";
import type { AccountResponse } from "@/lib/types";

/** Where an unauthenticated visitor is allowed to be. */
const PUBLIC_ROUTES = ["/sign-in", "/sign-up"];

type SessionStatus =
  | "loading"
  /** Checked, and nobody is signed in. */
  | "anonymous"
  | "signed-in"
  /** The backend did not answer at all. Distinct from "anonymous" because
   *  sending someone to a sign-in form they cannot submit is a worse answer
   *  than telling them the backend is down. */
  | "unreachable";

interface Session {
  status: SessionStatus;
  account: AccountResponse | null;
  /** Nobody has signed up on this backend yet, so the door says "create your
   *  account" instead of "sign in". */
  needsFirstAccount: boolean;
  signIn: (email: string, password: string) => Promise<void>;
  signUp: (email: string, password: string, displayName: string) => Promise<void>;
  signOut: () => Promise<void>;
  refresh: () => Promise<void>;
}

const SessionContext = createContext<Session | null>(null);

export function useSession(): Session {
  const session = useContext(SessionContext);
  if (session === null) throw new Error("useSession must be used inside SessionProvider");
  return session;
}

/**
 * Who is signed in, resolved once and shared.
 *
 * The session itself lives in an httpOnly cookie the browser attaches and this
 * code cannot read, which is the point: an XSS bug should not be able to lift
 * it. So "am I signed in" is a question only the backend can answer, and this
 * asks it once on mount rather than letting every page find out separately by
 * getting a 401.
 *
 * ## The redirect belongs here
 *
 * A guard per page is a guard somebody forgets on the page they add next. One
 * effect, above the router, sends an anonymous visitor to the door and a
 * signed-in one away from it.
 */
export function SessionProvider({ children }: { children: React.ReactNode }) {
  const [status, setStatus] = useState<SessionStatus>("loading");
  const [account, setAccount] = useState<AccountResponse | null>(null);
  const [needsFirstAccount, setNeedsFirstAccount] = useState(false);
  const router = useRouter();
  const pathname = usePathname();

  /** Ask the backend who this is. Returns what to believe, and touches no
   *  state itself, so the caller decides whether the answer is still wanted. */
  const read = useCallback(async (signal: AbortSignal) => {
    // Health first: it answers without a session and tells us whether this is
    // a brand-new backend, which decides what the door should say.
    let firstAccount = false;
    try {
      firstAccount = (await getHealth(signal)).needs_first_account;
    } catch (err) {
      if (err instanceof BackendUnreachableError) return { status: "unreachable" } as const;
      throw err;
    }
    try {
      const found = await whoami(signal);
      return { status: "signed-in", account: found, firstAccount } as const;
    } catch (err) {
      if (err instanceof NotSignedInError) {
        return { status: "anonymous", firstAccount } as const;
      }
      if (err instanceof BackendUnreachableError) return { status: "unreachable" } as const;
      throw err;
    }
  }, []);

  const apply = useCallback((result: Awaited<ReturnType<typeof read>>) => {
    if (result.status === "unreachable") {
      setStatus("unreachable");
      setAccount(null);
      return;
    }
    setNeedsFirstAccount(result.firstAccount);
    setAccount(result.status === "signed-in" ? result.account : null);
    setStatus(result.status);
  }, []);

  const refresh = useCallback(async () => {
    apply(await read(new AbortController().signal));
  }, [apply, read]);

  /** Prove the new session works before reporting it as one.
   *
   * Trusting the sign-in response body instead would show somebody a signed-in
   * console whose every subsequent request is anonymous -- which is exactly
   * what a cookie that failed to stick looks like, and it is far better to
   * find that out here than three screens later.
   */
  const confirm = useCallback(async () => {
    const result = await read(new AbortController().signal);
    apply(result);
    if (result.status !== "signed-in") {
      throw new Error(
        "Signed in, but the session did not stick. Check that the console and the backend " +
          "are on the same host (both localhost, or both 127.0.0.1)."
      );
    }
  }, [apply, read]);

  useEffect(() => {
    // Aborted on unmount so a slow answer never lands on a gone component, and
    // so a fast remount in development does not apply two answers in order.
    const controller = new AbortController();
    void (async () => {
      try {
        const result = await read(controller.signal);
        if (!controller.signal.aborted) apply(result);
      } catch (err) {
        if (err instanceof DOMException && err.name === "AbortError") return;
        throw err;
      }
    })();
    return () => controller.abort();
  }, [apply, read]);

  const onPublicRoute = PUBLIC_ROUTES.includes(pathname);

  useEffect(() => {
    if (status === "anonymous" && !onPublicRoute) {
      router.replace(needsFirstAccount ? "/sign-up" : "/sign-in");
    }
    if (status === "signed-in" && onPublicRoute) {
      router.replace("/chat");
    }
  }, [needsFirstAccount, onPublicRoute, router, status]);

  const value = useMemo<Session>(
    () => ({
      status,
      account,
      needsFirstAccount,
      signIn: async (email, password) => {
        await postSignIn(email, password);
        await confirm();
        // The first thing after signing in is a fresh set of everything, and
        // Next caches route segments. Without this, a person who signs out and
        // back in as somebody else can be shown the previous account's lists
        // from cache before the first fetch lands.
        router.refresh();
      },
      signUp: async (email, password, displayName) => {
        await postSignUp(email, password, displayName);
        setNeedsFirstAccount(false);
        await confirm();
        router.refresh();
      },
      signOut: async () => {
        await postSignOut();
        setAccount(null);
        setStatus("anonymous");
        router.refresh();
      },
      refresh,
    }),
    [account, confirm, needsFirstAccount, refresh, router, status]
  );

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}
