"use client";

import { useEffect, useState } from "react";
import { ChevronRightIcon, RefreshCwIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { JsonView } from "@/components/trace/json-view";
import { getRunState } from "@/lib/api";
import { describeApiError } from "@/lib/errors";
import { cn } from "@/lib/utils";

/**
 * `psych.state()` on a page.
 *
 * Status is the projection shaped for a screen; this is the one the worker
 * itself folds, and it is here for the question status cannot answer: "what
 * does the runtime think is going on right now". Collapsed by default because
 * it is large and it is the last thing a person needs, and read on demand
 * rather than streamed because a person opening it wants a snapshot they can
 * scroll, not one that shifts under the cursor.
 */
export function RunStatePanel({ runId, live }: { runId: string; live: boolean }) {
  const [open, setOpen] = useState(false);
  const [state, setState] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    let cancelled = false;
    // Deferred a tick, as everywhere else in this codebase: this is the
    // effect reacting to `open`/`runId`/`tick`, not work owed to the commit
    // that flipped the disclosure open.
    const id = setTimeout(() => {
      setLoading(true);
      void getRunState(runId, controller.signal)
        .then((next) => {
          if (cancelled) return;
          setState(next);
          setError(null);
        })
        .catch((err) => {
          if (cancelled) return;
          if (err instanceof DOMException && err.name === "AbortError") return;
          setError(describeApiError(err));
        })
        .finally(() => {
          if (!cancelled) setLoading(false);
        });
    }, 0);
    return () => {
      cancelled = true;
      controller.abort();
      clearTimeout(id);
    };
  }, [open, runId, tick]);

  const summary = state === null ? null : summarise(state);

  return (
    <div className="flex flex-col overflow-hidden rounded-xl border border-border bg-surface/40">
      <div className="bar">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="flex min-w-0 items-center gap-1.5 rounded-sm text-body font-medium transition-colors hover:text-foreground"
        >
          <ChevronRightIcon className={cn("size-3.5 transition-transform", open && "rotate-90")} aria-hidden />
          {open ? "Hide the state" : "Show the state"}
        </button>
        {summary && (
          <span className="truncate text-micro text-muted-foreground">{summary}</span>
        )}
        {open && (
          <div className="ml-auto">
            <Button
              variant="ghost"
              size="sm"
              disabled={loading}
              onClick={() => setTick((n) => n + 1)}
              title={live ? "The run is still going; read it again" : "Read it again"}
            >
              <RefreshCwIcon className={cn("size-3.5", loading && "animate-spin")} />
              Refresh
            </Button>
          </div>
        )}
      </div>
      {open && (
        <div className="max-h-[32rem] overflow-y-auto px-4 py-3">
          {error && <p className="text-caption text-destructive">{error}</p>}
          {state && <JsonView value={state} />}
        </div>
      )}
    </div>
  );
}

function summarise(state: Record<string, unknown>): string {
  const count = (key: string) => {
    const value = state[key];
    if (Array.isArray(value)) return value.length;
    if (value && typeof value === "object") return Object.keys(value).length;
    return 0;
  };
  const parts = [
    `turn ${String(state.turn ?? 0)}`,
    `${count("open_tool_calls")} open call${count("open_tool_calls") === 1 ? "" : "s"}`,
    `${count("pending_steer") + count("pending_follow_up") + count("pending_next_run")} queued`,
    `${count("children")} child${count("children") === 1 ? "" : "ren"}`,
  ];
  if (typeof state.head_seq === "number") parts.push(`head ${state.head_seq}`);
  return parts.join(", ");
}
