"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowDownIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  GitBranchIcon,
  GitForkIcon,
} from "lucide-react";

import { AnswerBlock } from "@/components/chat/answer-block";
import { ApprovalCard } from "@/components/chat/approval-card";
import { ComponentBlock } from "@/components/chat/component-block";
import { CopyMessageButton } from "@/components/chat/copy-message";
import { PlanPanel } from "@/components/chat/plan-panel";
import { QuestionCard } from "@/components/chat/question-card";
import {
  buildExchanges,
  workShown,
  type Exchange,
} from "@/components/chat/exchange";
import { EndedNote, TimelineNote } from "@/components/chat/timeline-notes";
import { UserMessage } from "@/components/chat/user-message";
import { WorkSection } from "@/components/chat/work-section";
import { WorkingIndicator } from "@/components/chat/working-indicator";
import { Button } from "@/components/ui/button";
import type { ReaskTarget } from "@/components/chat/reask-dialog";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import type { BranchChoice } from "@/lib/branches";
import type { Conversation } from "@/lib/conversation";
import type {
  AnswerView,
  PendingApproval,
  PendingQuestion,
  Task,
} from "@/lib/types";
import type { Lifecycle } from "@/components/ui/status";

/** How close to the end still counts as reading the end. A person who has
 *  scrolled up to re-read something is not asking to be dragged back down
 *  every time a tool answers, and a chat that does that is unusable during a
 *  long run. */
const PINNED_SLACK_PX = 96;

interface ConversationTimelineProps {
  conversation: Conversation;
  /** The Run at the end of the chain: the only one that can still be waiting
   * on a decision or still be writing. */
  activeRunId: string | null;
  /** Where the active Run is. Null before the first status read. */
  lifecycle: Lifecycle | null;
  /** True while the active Run can still write Records. Only that Run has a
   * turn in progress; every other Run is settled history. */
  live: boolean;
  /** The library's own split of the active Run, once it has settled. The
   * Records already on screen say the same thing, so this changes nothing a
   * reader sees; it is the authority when it disagrees. */
  answer: AnswerView | null;
  pendingApproval: PendingApproval | null;
  pendingQuestion: PendingQuestion | null;
  /** The live plan, shown on the newest exchange only: it is one list for
   *  the Run, not one per message. */
  plan: Task[];
  onAnswer: (answers: Record<string, string>) => Promise<void>;
  onDecide: (approved: boolean) => Promise<void>;
  decidedByLabel: string;
  /** `RunStatus.failure_message` for the active Run. Preferred over the log's
   * own failure text, which is the raw one. */
  failureMessage: string | null;
  /** A message sent but not yet echoed back by the Run's log, so pressing
   * send puts the message on screen at once instead of after a round trip. */
  optimisticMessage: string | null;
  /** Ask one message again, either as a branch of this chat or as a fork into
   * a new one. Offered on every message that has a Run behind it, which is
   * every message a person typed. */
  onReask: (target: ReaskTarget) => void;
  /** The versions of one message, or null when it was only asked once. Null
   * everywhere until the run list has been read. */
  versionsOf: (runId: string) => BranchChoice | null;
  /** Open another version of a message: its branch, at wherever that branch
   * has got to. */
  onOpenVersion: (runId: string) => void;
}

export function ConversationTimeline({
  conversation,
  activeRunId,
  lifecycle,
  live,
  answer,
  pendingApproval,
  pendingQuestion,
  plan,
  onAnswer,
  onDecide,
  decidedByLabel,
  failureMessage,
  optimisticMessage,
  onReask,
  versionsOf,
  onOpenVersion,
}: ConversationTimelineProps) {
  const contentRef = useRef<HTMLDivElement>(null);
  const pinnedRef = useRef(true);
  const [pinned, setPinned] = useState(true);

  const exchanges = useMemo(
    () => buildExchanges(conversation, live ? activeRunId : null),
    [conversation, live, activeRunId],
  );

  // The scroll container belongs to `ScrollArea`, which owns its own viewport
  // element, so it is reached through the slot attribute that component sets
  // rather than through a ref this file could hold.
  useEffect(() => {
    const content = contentRef.current;
    const viewport = content?.closest<HTMLElement>(
      '[data-slot="scroll-area-viewport"]',
    );
    if (!content || !viewport) return;

    const measure = () => {
      const distance =
        viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight;
      const near = distance <= PINNED_SLACK_PX;
      pinnedRef.current = near;
      setPinned(near);
    };

    // Height, not record count, is what the view follows. An answer revealing
    // itself over a beat and a work section being opened both grow the page
    // without adding an item, and a follow keyed on items would sit still
    // through both and then jump.
    const observer = new ResizeObserver(() => {
      if (pinnedRef.current) viewport.scrollTop = viewport.scrollHeight;
    });
    observer.observe(content);
    viewport.addEventListener("scroll", measure, { passive: true });
    measure();

    return () => {
      observer.disconnect();
      viewport.removeEventListener("scroll", measure);
    };
  }, []);

  // A conversation just opened is read from its end, wherever the last one was
  // left. Without this, clicking a long conversation in the history list drops
  // you at the top of it.
  useEffect(() => {
    const viewport = contentRef.current?.closest<HTMLElement>(
      '[data-slot="scroll-area-viewport"]',
    );
    if (!viewport) return;
    pinnedRef.current = true;
    setPinned(true);
    viewport.scrollTop = viewport.scrollHeight;
  }, [activeRunId]);

  function jumpToLatest() {
    const viewport = contentRef.current?.closest<HTMLElement>(
      '[data-slot="scroll-area-viewport"]',
    );
    if (!viewport) return;
    viewport.scrollTo({ top: viewport.scrollHeight, behavior: "smooth" });
  }

  return (
    <div ref={contentRef} className="flex w-full flex-col gap-8 py-8">
      {exchanges.map((exchange) => (
        <ExchangeBlock
          key={exchange.key}
          exchange={exchange}
          live={live && exchange.runId === activeRunId}
          lifecycle={exchange.runId === activeRunId ? lifecycle : null}
          answer={exchange.runId === activeRunId ? answer : null}
          // Whether this answer is worth revealing at a reading rate is
          // decided by whether the Run was still working when the block first
          // rendered, and `AnswerBlock` freezes that at mount. No record of
          // which Runs have been seen is kept anywhere: a set like that is
          // exactly the state that goes wrong when a render is replayed, and
          // the question it answers is already in this prop.
          reveal={live && exchange.runId === activeRunId}
          pendingApproval={
            exchange.runId === activeRunId ? pendingApproval : null
          }
          pendingQuestion={
            exchange.runId === activeRunId ? pendingQuestion : null
          }
          plan={exchange.runId === activeRunId ? plan : []}
          onAnswer={onAnswer}
          onDecide={onDecide}
          decidedByLabel={decidedByLabel}
          failureMessage={
            exchange.runId === activeRunId ? failureMessage : null
          }
          onReask={onReask}
          versions={versionsOf(exchange.runId)}
          onOpenVersion={onOpenVersion}
        />
      ))}

      {optimisticMessage !== null && (
        <div className="flex flex-col gap-4">
          <UserMessage text={optimisticMessage} />
          <WorkingIndicator turn={null} lifecycle="queued" />
        </div>
      )}

      {/* Sticky rather than fixed: it belongs to the conversation, so it
          floats above the last message and not over the composer. The wrapper
          is always rendered and always zero high, so the button appearing
          moves nothing. */}
      <div className="pointer-events-none sticky bottom-2 z-10 flex h-0 items-end justify-center">
        {!pinned && (
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={jumpToLatest}
            className="pointer-events-auto rounded-full bg-background elevated"
          >
            <ArrowDownIcon />
            Latest
          </Button>
        )}
      </div>
    </div>
  );
}

function ExchangeBlock({
  exchange,
  live,
  lifecycle,
  answer,
  reveal,
  pendingApproval,
  pendingQuestion,
  plan,
  onAnswer,
  onDecide,
  decidedByLabel,
  failureMessage,
  onReask,
  versions,
  onOpenVersion,
}: {
  exchange: Exchange;
  /** This exchange's Run is the active one and can still write Records. */
  live: boolean;
  lifecycle: Lifecycle | null;
  answer: AnswerView | null;
  reveal: boolean;
  pendingApproval: PendingApproval | null;
  pendingQuestion: PendingQuestion | null;
  /** The live plan, shown on the newest exchange only: it is one list for
   *  the Run, not one per message. */
  plan: Task[];
  onAnswer: (answers: Record<string, string>) => Promise<void>;
  onDecide: (approved: boolean) => Promise<void>;
  decidedByLabel: string;
  failureMessage: string | null;
  onReask: (target: ReaskTarget) => void;
  versions: BranchChoice | null;
  onOpenVersion: (runId: string) => void;
}) {
  const messageRef = useRef<HTMLDivElement>(null);

  // The library's text wins when it has been read. It is the same text: both
  // it and the block below come from the turn the loop finished on.
  const answerText =
    answer !== null && answer.finished
      ? answer.text
      : (exchange.answer?.text ?? "");
  const hasAnswer = exchange.answer !== null && answerText.trim().length > 0;

  // A Run whose log holds neither an answer nor an ending, before the first
  // status read has said which it is. Treated as working, because the other
  // reading -- a settled Run with nothing to show -- is the one that puts a
  // permanent blank where a sentence should be.
  const undecided =
    lifecycle === null && exchange.answer === null && exchange.ended === null;
  // A Run waiting on a decision is not working, whatever else is true: the
  // approval card above is the state, and a "Thinking" line under a question
  // nobody has answered is the app talking over itself.
  const working = !hasAnswer && lifecycle !== "waiting" && (live || undecided);

  return (
    <article className="group/exchange flex w-full flex-col gap-4">
      {/* Both affordances sit on the message rather than on the answer,
          because that is where they act: the new Run continues this message's
          predecessor, so this answer and everything after it are what gets
          left behind. Hovering the exchange reveals them; permanent buttons on
          every message make a timeline of buttons.

          Icons rather than words, and the two icons are the whole explanation
          people need once they have used either once: branch keeps the other
          version here, fork takes it away to a chat of its own. The version
          arrows are not hidden on hover -- they are state rather than an
          action, and a message that quietly has two answers is worse than a
          slightly busier line. */}
      {exchange.message && (
        <div className="flex w-full flex-col items-end gap-1">
          <UserMessage text={exchange.message.text} contentRef={messageRef} />
          <div className="flex items-center gap-0.5">
            <CopyMessageButton
              source={messageRef}
              text={exchange.message.text}
            />
            {exchange.runId !== "" && (
              <>
                {versions !== null && (
                  <VersionPager
                    versions={versions}
                    onOpenVersion={onOpenVersion}
                  />
                )}
                <MessageAction
                  label="Ask again in this chat"
                  icon={<GitBranchIcon className="size-3.5" aria-hidden />}
                  onClick={() =>
                    onReask({
                      runId: exchange.runId,
                      message: exchange.message?.text ?? "",
                      mode: "branch",
                    })
                  }
                />
                <MessageAction
                  label="Fork into a new chat"
                  icon={<GitForkIcon className="size-3.5" aria-hidden />}
                  onClick={() =>
                    onReask({
                      runId: exchange.runId,
                      message: exchange.message?.text ?? "",
                      mode: "fork",
                    })
                  }
                />
              </>
            )}
          </div>
        </div>
      )}

      {exchange.notes.map((note) => (
        <TimelineNote key={note.key} item={note} />
      ))}

      {/* Above the work, not inside it. The plan is what the agent said it
          would do; the work is what it did. Folding the plan into the
          collapsed section would hide the one thing that makes a long job
          readable while it is still running. */}
      {plan.length > 0 && <PlanPanel tasks={plan} />}

      <WorkSection
        work={workShown(exchange)}
        summary={answer !== null ? answer.summary : null}
      />

      {/* Below the work and above the answer, in the order the agent showed
          them. A component is addressed to the person rather than to the
          agent, so it stays outside the collapsed work section; it sits above
          the answer because the sentence usually refers to it ("this one fits
          your budget"). */}
      {exchange.components.map((item) => (
        <ComponentBlock key={item.key} component={item.component} />
      ))}

      {exchange.approvals.map((item) => (
        <ApprovalCard
          key={item.key}
          item={item}
          pending={item.approved === null ? pendingApproval : null}
          onDecide={onDecide}
          decidedByLabel={decidedByLabel}
        />
      ))}

      {/* Never both: the backend returns `pending_approval` as null while a
          question is pending, so the two cards cannot appear together and
          offer a person a verdict where words are wanted. */}
      {pendingQuestion !== null && (
        <QuestionCard pending={pendingQuestion} onAnswer={onAnswer} />
      )}

      {/* One slot, two states. The indicator stands where the answer will
          stand, in the same column and on the same line, so the answer
          arriving replaces a line rather than inserting one. */}
      {hasAnswer ? (
        <AnswerBlock text={answerText} reveal={reveal} />
      ) : working ? (
        <WorkingIndicator turn={exchange.current} lifecycle={lifecycle} />
      ) : null}

      {exchange.ended && (
        <EndedNote item={exchange.ended} failureMessage={failureMessage} />
      )}
    </article>
  );
}

/**
 * One icon button on a message, revealed by hovering the exchange it belongs
 * to and always reachable by keyboard.
 *
 * The name is on the button rather than in the tooltip alone, because an icon
 * with no accessible name is a button nobody using a screen reader can act on,
 * and a tooltip is not that name.
 */
function MessageAction({
  label,
  icon,
  onClick,
}: {
  label: string;
  icon: React.ReactNode;
  onClick: () => void;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          type="button"
          size="icon"
          variant="ghost"
          aria-label={label}
          className="size-7 text-muted-foreground opacity-0 transition-opacity group-hover/exchange:opacity-100 focus-visible:opacity-100"
          onClick={onClick}
        >
          {icon}
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  );
}

/**
 * `1/2` and a pair of arrows on a message that was asked more than one way.
 *
 * The arrows step through the versions in the order they were asked, and
 * opening one lands at the end of its branch rather than on the message
 * itself, because the reason to switch is to read the answer it led to. The
 * ends are disabled rather than wrapping: a pager that loops makes a person
 * count to know where they are.
 */
function VersionPager({
  versions,
  onOpenVersion,
}: {
  versions: BranchChoice;
  onOpenVersion: (runId: string) => void;
}) {
  const { versions: all, index } = versions;
  return (
    <div className="flex items-center gap-0.5 text-micro text-muted-foreground">
      <Button
        type="button"
        size="icon"
        variant="ghost"
        aria-label="Previous version of this message"
        className="size-7"
        disabled={index <= 0}
        onClick={() => onOpenVersion(all[index - 1].run_id)}
      >
        <ChevronLeftIcon className="size-3.5" aria-hidden />
      </Button>
      <span className="tabular-nums">
        {index + 1}/{all.length}
      </span>
      <Button
        type="button"
        size="icon"
        variant="ghost"
        aria-label="Next version of this message"
        className="size-7"
        disabled={index < 0 || index >= all.length - 1}
        onClick={() => onOpenVersion(all[index + 1].run_id)}
      >
        <ChevronRightIcon className="size-3.5" aria-hidden />
      </Button>
    </div>
  );
}
