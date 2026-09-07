"use client";

import { useMemo, useState } from "react";
import { GitCompareArrowsIcon, MaximizeIcon, MinimizeIcon, WrenchIcon } from "lucide-react";

import { CopyButton } from "@/components/activity/copyable";
import {
  collapseUnchanged,
  countChanges,
  diffLines,
  isTruncatedDiff,
} from "@/components/trace/prompt-diff";
import type { TurnContext } from "@/components/trace/trace-model";
import { cn } from "@/lib/utils";

/**
 * What this turn was told, and what it was offered, side by side.
 *
 * This is the panel the trace existed without: the prompt as actually sent,
 * per turn, next to the tool list that went with it. Before the runtime
 * recorded them, the only prompt anywhere in the product was one rebuilt from
 * the agent's definition, which by construction could not show what assembly
 * added: a tool withheld by an approval selector, a server that failed to
 * connect, a deferred catalogue, the line explaining what each connected
 * system is for. Those are the lines that change between turns, and they are
 * the answer to nearly every "why did it do that".
 */
export function TurnContextPanels({ context }: { context: TurnContext }) {
  return (
    <div className="grid min-w-0 gap-3 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
      <SystemPromptPanel context={context} />
      <ToolsOfferedPanel context={context} />
    </div>
  );
}

/** The one-word verdict on this turn's prompt, in the row list and the panel
 *  header. `changed` is the case worth hunting for, so it is the only one
 *  that gets a colour. */
export function promptChangeLabel(context: TurnContext): string {
  switch (context.change) {
    case "unrecorded":
      return "not recorded";
    case "first":
      return "first prompt";
    case "same":
      return "same as previous turn";
    case "changed":
      return context.previousTurn === null
        ? "changed this turn"
        : `changed since turn ${context.previousTurn}`;
  }
}

function Panel({
  title,
  icon: Icon,
  actions,
  children,
  className,
}: {
  title: string;
  icon: React.ComponentType<{ className?: string }>;
  actions?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section
      className={cn(
        "flex min-w-0 flex-col overflow-hidden rounded-lg border border-border bg-surface/40",
        className
      )}
    >
      {/* `.bar` rather than local padding: this header sits beside another
          one, and two headers with the same padding but different contents
          drew their bottom borders a few pixels apart. */}
      <header className="bar bg-surface/60">
        <Icon className="size-3.5 shrink-0 text-muted-foreground" />
        <h3 className="truncate text-caption font-medium">{title}</h3>
        <div className="ml-auto flex shrink-0 items-center gap-1">{actions}</div>
      </header>
      {children}
    </section>
  );
}

function ChangeBadge({ context }: { context: TurnContext }) {
  const changed = context.change === "changed";
  return (
    <span
      className={cn(
        "shrink-0 rounded-full px-2 py-0.5 text-micro font-medium",
        changed
          ? "bg-status-waiting/15 text-status-waiting"
          : "bg-muted text-muted-foreground"
      )}
    >
      {promptChangeLabel(context)}
    </span>
  );
}

function SystemPromptPanel({ context }: { context: TurnContext }) {
  const [expanded, setExpanded] = useState(false);
  const [showDiff, setShowDiff] = useState(false);

  const canDiff = context.change === "changed" && context.previousPrompt !== null;
  const diff = useMemo(
    () =>
      canDiff && context.previousPrompt !== null
        ? collapseUnchanged(diffLines(context.previousPrompt, context.prompt))
        : [],
    [canDiff, context.previousPrompt, context.prompt]
  );
  const changes = useMemo(
    () =>
      canDiff && context.previousPrompt !== null
        ? countChanges(diffLines(context.previousPrompt, context.prompt))
        : { added: 0, removed: 0 },
    [canDiff, context.previousPrompt, context.prompt]
  );
  const coarse =
    canDiff && context.previousPrompt !== null
      ? isTruncatedDiff(context.previousPrompt, context.prompt)
      : false;

  const lineCount = context.prompt.length === 0 ? 0 : context.prompt.split("\n").length;

  return (
    <Panel
      title="What the model was told"
      icon={GitCompareArrowsIcon}
      actions={
        context.change === "unrecorded" ? null : (
          <>
            {canDiff && (
              <button
                type="button"
                onClick={() => setShowDiff((v) => !v)}
                aria-pressed={showDiff}
                className={cn(
                  "rounded-md px-1.5 py-1 text-caption transition-colors hover:bg-muted",
                  showDiff ? "text-foreground" : "text-muted-foreground"
                )}
              >
                {showDiff ? "Full text" : "What changed"}
              </button>
            )}
            <CopyButton value={context.prompt} label="Copy" />
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              aria-label={expanded ? "Collapse the prompt" : "Expand the prompt"}
              className="rounded-md p-1 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
            >
              {expanded ? (
                <MinimizeIcon className="size-3.5" />
              ) : (
                <MaximizeIcon className="size-3.5" />
              )}
            </button>
          </>
        )
      }
    >
      <div className="flex flex-wrap items-center gap-2 border-b border-border/60 px-3 py-1.5">
        <ChangeBadge context={context} />
        {context.change !== "unrecorded" && (
          <span className="tabular text-micro text-muted-foreground">
            {lineCount.toLocaleString()} line{lineCount === 1 ? "" : "s"} ·{" "}
            {context.prompt.length.toLocaleString()} characters
          </span>
        )}
        {canDiff && (
          <span className="tabular ml-auto text-micro text-muted-foreground">
            <span className="text-status-done">+{changes.added}</span>{" "}
            <span className="text-status-failed">-{changes.removed}</span> line
            {changes.added + changes.removed === 1 ? "" : "s"}
          </span>
        )}
      </div>

      {context.change === "unrecorded" ? (
        // An empty `system_prompt` means this run predates the runtime
        // capturing it, not that the model was sent nothing. Rendering an
        // empty panel would read as the second.
        <p className="px-3 py-4 text-caption text-muted-foreground">
          Not recorded. This run was dispatched before the runtime kept the prompt as sent, so
          there is no text to show. Runs dispatched since carry theirs.
        </p>
      ) : showDiff ? (
        <div
          className={cn(
            "overflow-auto px-3 py-2 font-technical text-caption",
            expanded ? "max-h-[36rem]" : "max-h-72"
          )}
        >
          {coarse && (
            <p className="mb-2 text-micro text-muted-foreground italic">
              This prompt is too long to align line by line, so it is shown as the whole previous
              text replaced by the whole new one.
            </p>
          )}
          {diff.map((line, i) =>
            line.kind === "gap" ? (
              <div
                key={`gap:${i}`}
                className="my-1 border-y border-dashed border-border py-0.5 text-micro text-muted-foreground"
              >
                {line.hidden.toLocaleString()} unchanged line
                {line.hidden === 1 ? "" : "s"}
              </div>
            ) : (
              <div
                key={`${line.kind}:${i}`}
                className={cn(
                  "-mx-1 flex gap-2 rounded-sm px-1 whitespace-pre-wrap",
                  line.kind === "added" && "bg-status-done/10 text-foreground",
                  line.kind === "removed" && "bg-status-failed/10 text-muted-foreground line-through",
                  line.kind === "same" && "text-muted-foreground"
                )}
              >
                <span
                  aria-hidden
                  className={cn(
                    "w-3 shrink-0 select-none",
                    line.kind === "added" && "text-status-done",
                    line.kind === "removed" && "text-status-failed"
                  )}
                >
                  {line.kind === "added" ? "+" : line.kind === "removed" ? "-" : " "}
                </span>
                <span className="min-w-0">{line.text === "" ? " " : line.text}</span>
              </div>
            )
          )}
        </div>
      ) : (
        <pre
          tabIndex={0}
          className={cn(
            "overflow-y-auto px-3 py-2 font-technical text-caption whitespace-pre-wrap text-foreground/90",
            expanded ? "max-h-[36rem]" : "max-h-72"
          )}
        >
          {context.prompt}
        </pre>
      )}
    </Panel>
  );
}

function ToolChip({
  name,
  tone = "default",
}: {
  name: string;
  tone?: "default" | "added" | "removed";
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 font-technical text-micro",
        tone === "default" && "border-border bg-background text-foreground/80",
        tone === "added" && "border-status-done/40 bg-status-done/10 text-status-done",
        tone === "removed" &&
          "border-status-failed/40 bg-status-failed/10 text-status-failed line-through"
      )}
    >
      {name}
    </span>
  );
}

function ToolsOfferedPanel({ context }: { context: TurnContext }) {
  const added = new Set(context.toolsAdded);

  return (
    <Panel
      title="Tools offered this turn"
      icon={WrenchIcon}
      actions={
        context.toolNames.length > 0 ? (
          <span className="tabular text-micro text-muted-foreground">
            {context.toolNames.length}
          </span>
        ) : null
      }
    >
      {context.toolNames.length === 0 ? (
        <p className="px-3 py-4 text-caption text-muted-foreground">
          {context.change === "unrecorded"
            ? "Not recorded. This run predates the runtime keeping a per turn tool list."
            : "None. This turn could answer, and nothing else."}
        </p>
      ) : (
        <div className="flex flex-col gap-2 px-3 py-2.5">
          <div className="flex flex-wrap gap-1">
            {context.toolNames.map((name) => (
              <ToolChip key={name} name={name} tone={added.has(name) ? "added" : "default"} />
            ))}
          </div>
          {context.toolsRemoved.length > 0 && (
            <div className="flex flex-col gap-1 border-t border-border/60 pt-2">
              {/* A tool the previous turn had and this one does not is the
                  usual reason a model stops calling it, and the log is the
                  only place that fact exists. */}
              <span className="text-micro font-medium tracking-wide text-muted-foreground uppercase">
                Withheld since the previous turn
              </span>
              <div className="flex flex-wrap gap-1">
                {context.toolsRemoved.map((name) => (
                  <ToolChip key={name} name={name} tone="removed" />
                ))}
              </div>
            </div>
          )}
          {context.toolsAdded.length > 0 && (
            <p className="text-micro text-muted-foreground">
              Green tools were not offered on the previous turn.
            </p>
          )}
        </div>
      )}
    </Panel>
  );
}
