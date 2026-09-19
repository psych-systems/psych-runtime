"use client";

import { useEffect, useState } from "react";
import {
  BellIcon,
  CheckIcon,
  ClockIcon,
  OctagonPauseIcon,
  SendIcon,
  ShieldQuestionMarkIcon,
  XIcon,
} from "lucide-react";

import { deliverEvent, resumeRun } from "@/lib/api";
import { describeApiError } from "@/lib/errors";
import { formatClockTime } from "@/lib/format";
import type { PendingApproval, PendingQuestion, WaitingStep } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { QuestionCard } from "@/components/chat/question-card";
import { FieldError } from "@/components/settings/validation";

/**
 * What a parked workflow needs before it can carry on.
 *
 * Six reasons a run stops, and each of them takes a different answer: a
 * decision, some words, an event, or simply "go on". They are one panel
 * because a person looking at a stopped workflow has exactly one question --
 * *what is it waiting for and what do I do about it* -- and the answer should
 * be in one place rather than spread across the graph.
 *
 * The question case reuses the chat's own card rather than growing a second
 * one. An answer to a workflow's `human` step and an answer to an agent's
 * question go to the same endpoint and take the same shape, and the property
 * worth preserving is the one that card's comment names: free text is always
 * allowed, whatever options were offered.
 */
export function WaitingPanel({
  runId,
  waiting,
  question,
  approval,
  payloadSchema,
  by,
  onDone,
}: {
  runId: string;
  waiting: WaitingStep;
  /** The structured question, when the status endpoint has one for this
   *  step. Null falls back to the prompt on `waiting` alone. */
  question: PendingQuestion | null;
  /** The tool and the exact arguments an approval would run. */
  approval: PendingApproval | null;
  /** What a `wait` step's event must carry, for the pre-filled editor. */
  payloadSchema: Record<string, unknown> | null;
  by: string;
  onDone: () => void;
}) {
  return (
    <div className="flex flex-col gap-3 rounded-xl border border-status-waiting/40 bg-status-waiting/5 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <Icon reason={waiting.reason} />
        <span className="text-body font-medium">{headline(waiting)}</span>
        <span className="font-technical text-caption text-muted-foreground">{waiting.name}</span>
      </div>

      {waiting.reason === "approval" && (
        <ApprovalControls
          runId={runId}
          waiting={waiting}
          approval={approval}
          by={by}
          onDone={onDone}
        />
      )}

      {waiting.reason === "question" &&
        (question ? (
          <QuestionCard
            pending={question}
            subject="The workflow"
            onAnswer={async (answers) => {
              // Both shapes: `answers` keyed by question, as the chat card
              // sends for an agent, and a plain `answer` when there was one
              // question, so a workflow can read `steps.<name>.output.answer`
              // without quoting the question text in a path.
              const values = Object.values(answers);
              const payload: Record<string, unknown> =
                values.length === 1 ? { answers, answer: values[0] } : { answers };
              await resumeRun(runId, { payload, by });
              onDone();
            }}
          />
        ) : (
          <FreeTextAnswer runId={runId} prompt={waiting.question} by={by} onDone={onDone} />
        ))}

      {waiting.reason === "external" && (
        <EventControls
          runId={runId}
          event={waiting.event ?? ""}
          payloadSchema={payloadSchema}
          by={by}
          onDone={onDone}
        />
      )}

      {waiting.reason === "timer" && (
        <TimerControls runId={runId} wakeAt={waiting.wake_at} by={by} onDone={onDone} />
      )}

      {waiting.reason === "breakpoint" && (
        <BreakpointControls runId={runId} by={by} onDone={onDone} />
      )}

      {waiting.reason === "children" && (
        <p className="text-caption text-muted-foreground">
          Waiting on the runs this step started. Nothing to do here; it carries on by itself.
        </p>
      )}

      {waiting.expires_at && (
        <p className="text-micro text-muted-foreground">
          Expires at {formatClockTime(waiting.expires_at)}.
        </p>
      )}
    </div>
  );
}

function Icon({ reason }: { reason: WaitingStep["reason"] }) {
  const className = "size-4 shrink-0 text-status-waiting";
  switch (reason) {
    case "approval":
      return <ShieldQuestionMarkIcon className={className} aria-hidden />;
    case "timer":
      return <ClockIcon className={className} aria-hidden />;
    case "breakpoint":
      return <OctagonPauseIcon className={className} aria-hidden />;
    case "external":
      return <BellIcon className={className} aria-hidden />;
    default:
      return <ShieldQuestionMarkIcon className={className} aria-hidden />;
  }
}

function headline(waiting: WaitingStep): string {
  switch (waiting.reason) {
    case "approval":
      return "Waiting for a decision";
    case "question":
      return waiting.question ?? "Waiting for an answer";
    case "external":
      return `Waiting for ${waiting.event ?? "an event"}`;
    case "timer":
      return waiting.wake_at
        ? `Sleeping until ${formatClockTime(waiting.wake_at)}`
        : "Sleeping";
    case "breakpoint":
      return `Paused before ${waiting.name}`;
    case "children":
      return "Waiting on the runs it started";
  }
}

/** A small hook for the three controls that all do "send something, then tell
 *  the page to re-read". Keeps the error next to the button rather than in a
 *  toast that a person has already stopped looking at. */
function useAction(onDone: () => void) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function run(key: string, work: () => Promise<unknown>) {
    setBusy(key);
    setError(null);
    try {
      await work();
      onDone();
    } catch (err) {
      setError(describeApiError(err));
    } finally {
      setBusy(null);
    }
  }

  return { busy, error, run };
}

function ApprovalControls({
  runId,
  waiting,
  approval,
  by,
  onDone,
}: {
  runId: string;
  waiting: WaitingStep;
  approval: PendingApproval | null;
  by: string;
  onDone: () => void;
}) {
  const { busy, error, run } = useAction(onDone);
  return (
    <div className="flex flex-col gap-2">
      {waiting.question && <p className="text-caption text-muted-foreground">{waiting.question}</p>}
      {approval && (
        // The exact call a yes would run, not a paraphrase of it: an approval
        // of arguments nobody saw is not an approval.
        <div className="flex flex-col gap-1 rounded-lg border border-border bg-card p-3">
          <span className="text-caption text-muted-foreground">
            Would call <span className="font-technical text-foreground">{approval.tool}</span> with
          </span>
          <pre className="font-technical overflow-x-auto text-caption">
            {JSON.stringify(approval.arguments, null, 2)}
          </pre>
        </div>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          disabled={busy !== null}
          onClick={() => void run("yes", () => resumeRun(runId, { approved: true, by }))}
        >
          <CheckIcon />
          {busy === "yes" ? "Approving" : "Approve"}
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={busy !== null}
          onClick={() => void run("no", () => resumeRun(runId, { approved: false, by }))}
        >
          <XIcon />
          {busy === "no" ? "Denying" : "Deny"}
        </Button>
        <span className="text-micro text-muted-foreground">Decided as {by}</span>
      </div>
      <FieldError message={error ?? undefined} />
    </div>
  );
}

function FreeTextAnswer({
  runId,
  prompt,
  by,
  onDone,
}: {
  runId: string;
  prompt: string | null;
  by: string;
  onDone: () => void;
}) {
  const [text, setText] = useState("");
  const { busy, error, run } = useAction(onDone);
  return (
    <div className="flex flex-col gap-2">
      {prompt && <p className="text-caption text-muted-foreground">{prompt}</p>}
      <Textarea
        aria-label="Your answer"
        value={text}
        rows={3}
        placeholder="Type your answer"
        onChange={(event) => setText(event.target.value)}
      />
      <div>
        <Button
          size="sm"
          disabled={busy !== null || text.trim() === ""}
          onClick={() =>
            void run("send", () => resumeRun(runId, { payload: { answer: text.trim() }, by }))
          }
        >
          <SendIcon />
          {busy === "send" ? "Sending" : "Send"}
        </Button>
      </div>
      <FieldError message={error ?? undefined} />
    </div>
  );
}

/**
 * The event a `wait` step is listening for.
 *
 * The payload box starts filled from the step's own schema, because an empty
 * `{}` against a schema with three required fields is four rejections before
 * anybody gets it right. What checking there is happens here rather than only
 * at the backend, for the same reason: a missing field named on the screen
 * beats a 422.
 */
function EventControls({
  runId,
  event,
  payloadSchema,
  by,
  onDone,
}: {
  runId: string;
  event: string;
  payloadSchema: Record<string, unknown> | null;
  by: string;
  onDone: () => void;
}) {
  const [text, setText] = useState(() => JSON.stringify(skeleton(payloadSchema), null, 2));
  const { busy, error, run } = useAction(onDone);
  const [problem, setProblem] = useState<string | null>(null);

  // A schema that arrives after the first render (the view lands before the
  // status does) still gets to fill an untouched box. Done while rendering,
  // not in an effect, so the empty box is never shown first.
  const [seenSchema, setSeenSchema] = useState(payloadSchema);
  if (payloadSchema !== seenSchema) {
    setSeenSchema(payloadSchema);
    if (text.trim() === "" || text.trim() === "{}") {
      setText(JSON.stringify(skeleton(payloadSchema), null, 2));
    }
  }

  function send() {
    let payload: Record<string, unknown>;
    try {
      const parsed: unknown = text.trim() === "" ? {} : JSON.parse(text);
      if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
        setProblem("The payload is an object of fields.");
        return;
      }
      payload = parsed as Record<string, unknown>;
    } catch (err) {
      setProblem(err instanceof Error ? err.message : "Not valid JSON.");
      return;
    }
    const complaint = checkAgainstSchema(payload, payloadSchema);
    if (complaint !== null) {
      setProblem(complaint);
      return;
    }
    setProblem(null);
    void run("deliver", () => deliverEvent(runId, { event, payload, by }));
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="wf-event-payload">
          What <span className="font-technical">{event || "the event"}</span> carries
        </Label>
        <Textarea
          id="wf-event-payload"
          value={text}
          rows={6}
          spellCheck={false}
          aria-invalid={problem !== null || undefined}
          className="font-technical text-caption"
          onChange={(change) => {
            setText(change.target.value);
            setProblem(null);
          }}
        />
      </div>
      <div>
        <Button size="sm" disabled={busy !== null || event === ""} onClick={send}>
          <SendIcon />
          {busy === "deliver" ? "Delivering" : "Deliver"}
        </Button>
      </div>
      <FieldError message={problem ?? error ?? undefined} />
    </div>
  );
}

/** An example body from a JSON Schema: every declared property with a value
 *  of the right sort, so the shape is right and only the values need typing. */
function skeleton(schema: Record<string, unknown> | null): Record<string, unknown> {
  const properties = (schema as { properties?: Record<string, unknown> } | null)?.properties;
  if (!properties) return {};
  const out: Record<string, unknown> = {};
  for (const [key, raw] of Object.entries(properties)) {
    const type = (raw as { type?: string } | null)?.type;
    out[key] =
      type === "number" || type === "integer"
        ? 0
        : type === "boolean"
          ? false
          : type === "array"
            ? []
            : type === "object"
              ? {}
              : "";
  }
  return out;
}

/**
 * Required fields and declared types, checked here.
 *
 * Only what a JSON Schema states plainly: which fields must be present, and
 * what sort each declared one is. Anything deeper is the backend's job, and
 * guessing at it here would reject a payload the backend would have accepted.
 */
function checkAgainstSchema(
  payload: Record<string, unknown>,
  schema: Record<string, unknown> | null
): string | null {
  if (schema === null) return null;
  const required = Array.isArray(schema.required) ? (schema.required as unknown[]) : [];
  const missing = required
    .filter((one): one is string => typeof one === "string")
    .filter((one) => !(one in payload));
  if (missing.length > 0) {
    return `${missing.join(", ")} ${missing.length === 1 ? "is" : "are"} required.`;
  }

  const properties = (schema.properties ?? {}) as Record<string, unknown>;
  for (const [key, value] of Object.entries(payload)) {
    const declared = (properties[key] as { type?: string } | undefined)?.type;
    if (declared === undefined) continue;
    if (!matchesType(value, declared)) {
      return `${key} should be ${declared === "integer" ? "a whole number" : `a ${declared}`}.`;
    }
  }
  return null;
}

function matchesType(value: unknown, type: string): boolean {
  switch (type) {
    case "string":
      return typeof value === "string";
    case "number":
      return typeof value === "number";
    case "integer":
      return typeof value === "number" && Number.isInteger(value);
    case "boolean":
      return typeof value === "boolean";
    case "array":
      return Array.isArray(value);
    case "object":
      return value !== null && typeof value === "object" && !Array.isArray(value);
    case "null":
      return value === null;
    default:
      return true;
  }
}

/** A sleeping step: when it wakes, how long that is, and the button that does
 *  not wait. */
function TimerControls({
  runId,
  wakeAt,
  by,
  onDone,
}: {
  runId: string;
  wakeAt: string | null;
  by: string;
  onDone: () => void;
}) {
  const { busy, error, run } = useAction(onDone);
  const remaining = useCountdown(wakeAt);

  return (
    <div className="flex flex-col gap-2">
      {remaining !== null && (
        <p className="tabular text-caption text-muted-foreground">
          {remaining > 0 ? `${formatRemaining(remaining)} to go.` : "Due now."}
        </p>
      )}
      <div>
        <Button
          size="sm"
          variant="outline"
          disabled={busy !== null}
          onClick={() => void run("wake", () => resumeRun(runId, { payload: {}, by }))}
        >
          <ClockIcon />
          {busy === "wake" ? "Waking" : "Wake now"}
        </Button>
      </div>
      <FieldError message={error ?? undefined} />
    </div>
  );
}

function useCountdown(iso: string | null): number | null {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (iso === null) return;
    const timer = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(timer);
  }, [iso]);
  if (iso === null) return null;
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return null;
  return Math.max(0, Math.round((at - now) / 1000));
}

function formatRemaining(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

/**
 * A run parked on a breakpoint.
 *
 * Three buttons, because there are three things a person wants from here and
 * only one of them is "go". Continue takes the next step under whatever
 * stepping the run already has; the other two say what stepping to use from
 * now on, so somebody who has seen enough can stop being asked, and somebody
 * who wants to watch closely can start being asked at every step.
 */
function BreakpointControls({
  runId,
  by,
  onDone,
}: {
  runId: string;
  by: string;
  onDone: () => void;
}) {
  const { busy, error, run } = useAction(onDone);
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          disabled={busy !== null}
          onClick={() => void run("go", () => resumeRun(runId, { payload: {}, by }))}
        >
          {busy === "go" ? "Continuing" : "Continue"}
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={busy !== null}
          onClick={() =>
            void run("run", () => resumeRun(runId, { payload: { step_mode: false }, by }))
          }
        >
          {busy === "run" ? "Continuing" : "Continue without stopping"}
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={busy !== null}
          onClick={() =>
            void run("step", () => resumeRun(runId, { payload: { step_mode: true }, by }))
          }
        >
          {busy === "step" ? "Stepping" : "Step"}
        </Button>
      </div>
      <p className="text-micro text-muted-foreground">
        Step stops again before the next one. Continue without stopping runs to the end or to the
        next thing that needs you.
      </p>
      <FieldError message={error ?? undefined} />
    </div>
  );
}
