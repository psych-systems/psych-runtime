"use client";

import Link from "next/link";
import { useState } from "react";
import { ArrowLeftIcon } from "lucide-react";

import { RunViewSwitcher } from "@/components/activity/run-view-switcher";
import { Button } from "@/components/ui/button";
import { Page, PageHeader, Section, Stat } from "@/components/ui/page";
import { StatusPill } from "@/components/ui/status";
import { TraceTimeline, type TimeRange } from "@/components/trace/trace-timeline";
import { TraceWorkspace } from "@/components/trace/trace-workspace";
import type { TraceEntry } from "@/components/trace/trace-model";

const RUN_ID = "access-review-3";
const START = "2026-09-11T06:58:00.000Z";

const REVIEW_MESSAGES = [
  {
    asked: "List the accounts with elevated workspace access.",
    answered: "I found six accounts with administrator or billing access and grouped them by role.",
    time: "12:26 PM",
    modelCalls: 1,
    toolCalls: 1,
    tokens: "1,982",
    cost: "$0.0058",
    elapsed: "14.8s",
  },
  {
    asked: "Which of those accounts have been inactive for more than 60 days?",
    answered: "Two accounts match: one unused administrator and one inactive service identity with active credentials.",
    time: "12:27 PM",
    modelCalls: 0,
    toolCalls: 1,
    tokens: "814",
    cost: "$0.0019",
    elapsed: "12.1s",
  },
  {
    asked: "Summarize the access changes we should make.",
    answered: "Remove the unused administrator role, rotate and then disable the inactive service credential, and record both changes for review.",
    time: "12:28 PM",
    modelCalls: 1,
    toolCalls: 0,
    tokens: "2,158",
    cost: "$0.0078",
    elapsed: "18.3s",
  },
];

const ENTRIES: TraceEntry[] = [
  {
    key: "message-1", kind: "message", turn: 1, stepId: null,
    label: "Message 1", summary: "Review workspace access and flag accounts that need attention",
    status: "ok", willRetry: false, startedAt: START, finishedAt: "2026-09-11T06:58:14.800Z",
    timed: true, offsetSeconds: 0, durationSeconds: 14.8, ongoing: false,
    data: { runId: "access-review-1", index: 1, text: REVIEW_MESSAGES[0].asked },
  },
  {
    key: "model-1", kind: "model", turn: 1, stepId: null,
    label: "primary-model", summary: "1,846 in · 214 out · $0.0064 computed",
    status: "ok", willRetry: false, startedAt: "2026-09-11T06:58:00.200Z", finishedAt: "2026-09-11T06:58:07.570Z",
    timed: true, offsetSeconds: 0.2, durationSeconds: 7.37, ongoing: false,
    context: { prompt: "Review access carefully and explain every flagged account.", change: "first", previousPrompt: null, previousTurn: null, toolNames: ["list_accounts", "read_permissions"], toolsAdded: [], toolsRemoved: [] },
    data: { turn: 1, step_id: null, model: "primary-model", system_prompt: "Review access carefully and explain every flagged account.", tool_names: ["list_accounts", "read_permissions"], usage: { input: 1846, output: 214, cache_read: 0, cache_write: 0, cache_write_1h: 0, reasoning: 96 }, usage_reported: true, cost: { amount: "0.0064", currency: "USD", model: "primary-model", source: "computed" }, timings: { queue_wait_seconds: 0.04, time_to_first_token_seconds: 0.62, stream_duration_seconds: 6.71 }, finish_reason: "tool_calls", text: "", tool_call_ids: ["list-accounts"], started_at: "2026-09-11T06:58:00.200Z", finished_at: "2026-09-11T06:58:07.570Z", failure: null, will_retry: false, dangling: false },
  },
  {
    key: "tool-1", kind: "tool", turn: 1, stepId: null,
    label: "list_accounts", summary: "42 accounts returned",
    status: "ok", willRetry: false, startedAt: "2026-09-11T06:58:07.800Z", finishedAt: "2026-09-11T06:58:12.600Z",
    timed: true, offsetSeconds: 7.8, durationSeconds: 4.8, ongoing: false,
    data: { call_id: "list-accounts", tool: "list_accounts", turn: 1, step_id: null, arguments: { status: "active" }, outcome: "ok", result: { count: 42, reviewed: 42 }, failure: null, duration_seconds: 4.8, result_handle: null, attachments: [], preview: "42 accounts", result_bytes: 1840, started_at: "2026-09-11T06:58:07.800Z", finished_at: "2026-09-11T06:58:12.600Z", interruptible: true, safe_to_retry: true, parent_call_id: null },
  },
  {
    key: "message-2", kind: "message", turn: 1, stepId: null,
    label: "Message 2", summary: REVIEW_MESSAGES[1].asked,
    status: "ok", willRetry: false, startedAt: "2026-09-11T06:58:17.000Z", finishedAt: "2026-09-11T06:58:29.100Z",
    timed: true, offsetSeconds: 17, durationSeconds: 12.1, ongoing: false,
    data: { runId: "access-review-2", index: 2, text: REVIEW_MESSAGES[1].asked },
  },
  {
    key: "tool-2", kind: "tool", turn: 2, stepId: null,
    label: "read_permissions", summary: "Compared roles and last activity",
    status: "ok", willRetry: false, startedAt: "2026-09-11T06:58:18.100Z", finishedAt: "2026-09-11T06:58:24.000Z",
    timed: true, offsetSeconds: 18.1, durationSeconds: 5.9, ongoing: false,
    data: { call_id: "read-permissions", tool: "read_permissions", turn: 2, step_id: null, arguments: { account_ids: ["acct-17", "acct-31"] }, outcome: "ok", result: { flagged: 2, reasons: ["unused administrator role", "inactive service identity"] }, failure: null, duration_seconds: 5.9, result_handle: null, attachments: [], preview: "2 accounts flagged", result_bytes: 2260, started_at: "2026-09-11T06:58:18.100Z", finished_at: "2026-09-11T06:58:24.000Z", interruptible: true, safe_to_retry: true, parent_call_id: null },
  },
  {
    key: "message-3", kind: "message", turn: 1, stepId: null,
    label: "Message 3", summary: REVIEW_MESSAGES[2].asked,
    status: "ok", willRetry: false, startedAt: "2026-09-11T06:58:30.000Z", finishedAt: "2026-09-11T06:58:48.300Z",
    timed: true, offsetSeconds: 30, durationSeconds: 18.3, ongoing: false,
    data: { runId: RUN_ID, index: 3, text: REVIEW_MESSAGES[2].asked },
  },
  {
    key: "model-2", kind: "model", turn: 3, stepId: null,
    label: "primary-model", summary: "2,408 in · 486 out · $0.0091 computed",
    status: "ok", willRetry: false, startedAt: "2026-09-11T06:58:32.100Z", finishedAt: "2026-09-11T06:58:42.500Z",
    timed: true, offsetSeconds: 32.1, durationSeconds: 10.4, ongoing: false,
    context: { prompt: "Review access carefully and explain every flagged account.", change: "same", previousPrompt: "Review access carefully and explain every flagged account.", previousTurn: 1, toolNames: ["list_accounts", "read_permissions"], toolsAdded: [], toolsRemoved: [] },
    data: { turn: 3, step_id: null, model: "primary-model", system_prompt: "Review access carefully and explain every flagged account.", tool_names: ["list_accounts", "read_permissions"], usage: { input: 2408, output: 486, cache_read: 1220, cache_write: 0, cache_write_1h: 0, reasoning: 142 }, usage_reported: true, cost: { amount: "0.0091", currency: "USD", model: "primary-model", source: "computed" }, timings: { queue_wait_seconds: 0.03, time_to_first_token_seconds: 0.54, stream_duration_seconds: 9.83 }, finish_reason: "stop", text: "Two accounts need attention. One retains an unused administrator role; the other is an inactive service identity with active credentials.", tool_call_ids: [], started_at: "2026-09-11T06:58:32.100Z", finished_at: "2026-09-11T06:58:42.500Z", failure: null, will_retry: false, dangling: false },
  },
];

export function ReviewRunDetail() {
  return (
    <Page>
      <PageHeader
        title="access-reviewer"
        description="3 messages · started today at 12:26 PM"
        actions={<div className="flex items-center gap-2"><StatusPill state="done" /><RunViewSwitcher runId={RUN_ID} active="activity" review /></div>}
      />
      <Button asChild variant="ghost" size="sm" className="w-fit"><Link href="/conversations?review=1"><ArrowLeftIcon className="size-4" />All conversations</Link></Button>
      <Section title="Conversation overview" description="Totals across every message in this conversation.">
        <div className="grid gap-4 rounded-xl border border-border bg-card p-4 sm:grid-cols-4">
          <Stat label="Messages" value="3" />
          <Stat label="Model calls" value="2" />
          <Stat label="Tool calls" value="2" />
          <Stat label="Elapsed" value="48.3s" />
        </div>
      </Section>
      <div className="grid gap-4 lg:grid-cols-2">
        <ReviewBreakdownCard title="Tokens" total="4,954" hint="Provider reported" values={[["Input", "3,164"], ["Output", "570"], ["Cache read", "1,220"], ["Cache write", "0"]]} />
        <ReviewBreakdownCard title="Cost" total="$0.0155" hint="Estimated from rates" values={[["Input", "$0.0068"], ["Output", "$0.0075"], ["Cache read", "$0.0012"], ["Cache write", "$0.00"]]} />
      </div>
      <Section title="Conversation activity" description="Each message keeps its request, answer and operational cost together.">
        <div className="flex flex-col gap-3">
          {REVIEW_MESSAGES.map((message, index) => (
            <article key={message.asked} className="overflow-hidden rounded-xl border border-border bg-surface/40">
              <header className="flex flex-wrap items-center gap-3 border-b border-border px-4 py-3">
                <span className="font-semibold">Message {index + 1}</span>
                <StatusPill state="done" size="sm" />
                <span className="text-caption text-muted-foreground">Today at {message.time}</span>
                <Button asChild variant="ghost" size="sm" className="ml-auto"><Link href={`/activity/${RUN_ID}/trace?review=1`}>View in trace</Link></Button>
              </header>
              <div className="grid gap-5 p-4 lg:grid-cols-[minmax(0,1fr)_16rem]">
                <div className="flex min-w-0 flex-col gap-4">
                  <ReviewExchange label="Asked" text={message.asked} />
                  <ReviewExchange label="Answered" text={message.answered} />
                </div>
                <dl className="grid grid-cols-2 gap-x-5 gap-y-3 border-t border-border pt-4 text-caption lg:border-t-0 lg:border-l lg:pt-0 lg:pl-5">
                  <ReviewMetric label="Model calls" value={message.modelCalls.toString()} />
                  <ReviewMetric label="Tool calls" value={message.toolCalls.toString()} />
                  <ReviewMetric label="Total tokens" value={message.tokens} />
                  <ReviewMetric label="Total cost" value={message.cost} />
                  <ReviewMetric label="Elapsed" value={message.elapsed} />
                  <ReviewMetric label="Recovery" value="None" />
                </dl>
              </div>
            </article>
          ))}
        </div>
      </Section>
    </Page>
  );
}

function ReviewExchange({ label, text }: { label: string; text: string }) {
  return <div><p className="text-micro font-medium tracking-wide text-muted-foreground uppercase">{label}</p><p className="mt-1.5 text-prose">{text}</p></div>;
}

function ReviewMetric({ label, value }: { label: string; value: string }) {
  return <div><dt className="text-micro font-medium tracking-wide text-muted-foreground uppercase">{label}</dt><dd className="mt-0.5 tabular font-medium">{value}</dd></div>;
}

function ReviewBreakdownCard({ title, total, hint, values }: { title: string; total: string; hint: string; values: [string, string][] }) {
  return (
    <section className="rounded-xl border border-border bg-card p-4">
      <h2 className="text-base font-semibold">{title}</h2>
      <div className="mt-4 border-b border-border pb-4"><Stat label="Total" value={total} hint={hint} /></div>
      <dl className="mt-4 grid grid-cols-2 gap-x-5 gap-y-4">
        {values.map(([label, value]) => <ReviewMetric key={label} label={label} value={value} />)}
      </dl>
    </section>
  );
}

export function ReviewTraceDetail() {
  const [selectedKey, setSelectedKey] = useState(ENTRIES[0].key);
  const [range, setRange] = useState<TimeRange>({ start: 0, end: 48.3 });
  const selected = ENTRIES.find((entry) => entry.key === selectedKey) ?? ENTRIES[0];
  const visible = ENTRIES.filter((entry) => {
    if (entry.offsetSeconds === null || entry.durationSeconds === null) return true;
    return entry.offsetSeconds + entry.durationSeconds >= range.start && entry.offsetSeconds <= range.end;
  });
  return (
    <div className="flex h-full min-h-0 flex-1 flex-col">
      <div className="bar px-4">
        <Button asChild variant="ghost" size="icon-sm"><Link href="/conversations?review=1" aria-label="All conversations"><ArrowLeftIcon className="size-4" /></Link></Button>
        <span className="hidden text-caption font-medium sm:inline">access-reviewer</span>
        <div className="ml-auto flex shrink-0 items-center gap-2"><StatusPill state="done" size="sm" /><RunViewSwitcher runId={RUN_ID} active="trace" review /></div>
      </div>
      <div className="border-b border-border px-4 py-2 text-caption text-muted-foreground">3 messages <span aria-hidden>·</span> 2 model calls <span aria-hidden>·</span> 2 tool calls <span aria-hidden>·</span> 4,954 tokens, provider reported <span aria-hidden>·</span> $0.0155 estimated from rates <span aria-hidden>·</span> 48.3s</div>
      <TraceTimeline entries={ENTRIES} totalSeconds={48.3} range={range} selectedKey={selectedKey} onSelect={setSelectedKey} onRangeChange={setRange} />
      <TraceWorkspace entries={visible} selectedEntry={selected} selectedKey={selectedKey} onSelect={setSelectedKey} />
    </div>
  );
}
