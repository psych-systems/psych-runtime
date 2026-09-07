"use client";

import Link from "next/link";
import { useState } from "react";
import { Loader2Icon } from "lucide-react";

import { useSession } from "@/components/auth/session-provider";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { describeApiError } from "@/lib/errors";
import { Trident } from "@/components/brand/trident";

type Mode = "sign-in" | "sign-up";

const COPY = {
  "sign-in": {
    title: "Welcome back",
    action: "Sign in",
    working: "Signing in",
    alternative: "New here?",
    alternativeAction: "Create an account",
    alternativeHref: "/sign-up",
  },
  "sign-up": {
    title: "Create your account",
    action: "Create account",
    working: "Creating your account",
    alternative: "Already have an account?",
    alternativeAction: "Sign in",
    alternativeHref: "/sign-in",
  },
} as const;

/**
 * The door.
 *
 * One component for both modes because they differ by three strings and one
 * field, and two near-identical forms drift: the day somebody fixes the
 * autofill hint on one is the day the other keeps the bug.
 *
 * ## Why it says what it says
 *
 * On a fresh backend the first screen is sign-up, not sign-in. Somebody who
 * has just run the launcher has no account, and offering to authenticate them
 * against an empty list is a dead end dressed as a form.
 *
 * The password rule is stated *before* it is broken rather than only in the
 * error afterwards. A person choosing a password should know the constraint
 * while they are choosing.
 *
 * Failures land in one alert above the button rather than under the field that
 * caused them. Sign-in deliberately cannot say which field was wrong -- doing
 * so turns the form into an account-enumeration oracle -- so there is no field
 * to attach it to.
 */
export function AuthForm({ mode }: { mode: Mode }) {
  const copy = COPY[mode];
  const session = useSession();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (mode === "sign-up") {
        await session.signUp(email, password, displayName);
      } else {
        await session.signIn(email, password);
      }
      // No navigation here. `SessionProvider` moves a signed-in visitor off
      // the door, so routing lives in one place instead of in every form that
      // can create a session.
    } catch (err) {
      setError(describeApiError(err));
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-svh flex-col items-center justify-center px-6 py-12">
      <div className="w-full max-w-sm">
        <div className="mb-8 flex flex-col items-center gap-3 text-center">
          <span
            aria-hidden
            className="flex size-11 items-center justify-center rounded-xl bg-primary font-heading text-xl font-semibold text-primary-foreground"
          >
            <Trident className="size-7" />
          </span>
          <div>
            <h1 className="font-heading text-xl font-semibold tracking-tight">{copy.title}</h1>
            <p className="mt-1 text-caption text-muted-foreground">
              {mode === "sign-up"
                ? "Your agents, connections and conversations stay yours."
                : "Sign in to your Psych console."}
            </p>
          </div>
        </div>

        <form onSubmit={submit} className="flex flex-col gap-4">
          {mode === "sign-up" && (
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="display-name">
                Name <span className="text-muted-foreground">(optional)</span>
              </Label>
              <Input
                id="display-name"
                value={displayName}
                onChange={(event) => setDisplayName(event.target.value)}
                autoComplete="name"
                placeholder="What should we call you?"
              />
            </div>
          )}

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="email">Email</Label>
            <Input
              id="email"
              type="email"
              required
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              autoComplete="email"
              autoFocus={mode === "sign-in"}
              placeholder="you@example.com"
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="password">Password</Label>
            <Input
              id="password"
              type="password"
              required
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              autoComplete={mode === "sign-up" ? "new-password" : "current-password"}
            />
            {mode === "sign-up" && (
              <p className="text-micro text-muted-foreground">
                At least 12 characters. A phrase you can remember beats a short password you
                cannot.
              </p>
            )}
          </div>

          {error !== null && (
            <Alert variant="destructive">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}

          <Button type="submit" disabled={busy} className="mt-1 w-full">
            {busy && <Loader2Icon className="animate-spin" />}
            {busy ? copy.working : copy.action}
          </Button>
        </form>

        <p className="mt-6 text-center text-caption text-muted-foreground">
          {copy.alternative}{" "}
          <Link
            href={copy.alternativeHref}
            className="text-foreground underline underline-offset-2 hover:no-underline"
          >
            {copy.alternativeAction}
          </Link>
        </p>

        {mode === "sign-up" && (
          <p className="mt-6 text-center text-micro text-muted-foreground">
            This console stores what you configure on the machine running it, unencrypted. Fine
            for your own laptop; read the README before pointing anyone else at it.
          </p>
        )}
      </div>
    </div>
  );
}
