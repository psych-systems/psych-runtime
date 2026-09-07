"use client";

import { useState } from "react";
import { MessageCircleQuestionMarkIcon, SendIcon } from "lucide-react";

import type { AskedQuestion, PendingQuestion } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

/**
 * The agent asked something, and the conversation is stopped until it is
 * answered.
 *
 * The sibling of `approval-card.tsx`, and deliberately a different control.
 * An approval takes yes or no; a question takes words. The backend keeps them
 * apart too: `pending_approval` is `null` while a question is pending, so this
 * card and that one can never both appear.
 *
 * ## Options are the fast path, not the only path
 *
 * When the model names the branches it is choosing between, they render as
 * buttons and answering is one click. When it does not, or when none of them
 * is right, there is always a text box. Nothing validates an answer against
 * the options, which is what makes offering them safe: a model that guessed
 * the branches wrong has cost a click rather than the ability to answer.
 *
 * That mirrors how this tool is specified in `psych.core.questions`, and it is
 * the property to preserve if this card is ever redesigned. A picker with no
 * escape hatch turns a wrong guess by the model into a dead end for a person.
 */
export function QuestionCard({
  pending,
  onAnswer,
}: {
  pending: PendingQuestion;
  /** Resolves once the answer is delivered. Rejects with a message to show. */
  onAnswer: (answers: Record<string, string>) => Promise<void>;
}) {
  const [picked, setPicked] = useState<Record<number, string[]>>({});
  const [typed, setTyped] = useState<Record<number, string>>({});
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const questions = pending.questions.length > 0 ? pending.questions : fallback(pending);

  function answerFor(index: number): string {
    const chosen = picked[index] ?? [];
    const written = (typed[index] ?? "").trim();
    // Both, when somebody clicked an option and then added a note. Neither is
    // dropped: the note is usually the part that matters.
    return [chosen.join(", "), written].filter(Boolean).join(", ");
  }

  const answered = questions.every((_, index) => answerFor(index) !== "");

  function toggle(index: number, question: AskedQuestion, label: string) {
    setPicked((current) => {
      const chosen = current[index] ?? [];
      if (question.multi_select) {
        return {
          ...current,
          [index]: chosen.includes(label)
            ? chosen.filter((one) => one !== label)
            : [...chosen, label],
        };
      }
      // Clicking the chosen option again clears it, so a single-select is not
      // a trap once something has been picked.
      return { ...current, [index]: chosen[0] === label ? [] : [label] };
    });
  }

  async function send() {
    setSending(true);
    setError(null);
    try {
      await onAnswer(
        Object.fromEntries(
          questions.map((question, index) => [
            question.header || question.question,
            answerFor(index),
          ])
        )
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not send that answer.");
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="w-full rounded-xl border border-status-waiting/40 bg-status-waiting/5 p-3.5">
      <div className="flex items-start gap-2.5">
        <MessageCircleQuestionMarkIcon
          className="mt-0.5 size-4 shrink-0 text-status-waiting"
          aria-hidden
        />
        <div className="flex min-w-0 flex-1 flex-col gap-4">
          <p className="text-caption text-muted-foreground">
            {questions.length === 1
              ? "The agent needs an answer before it can carry on."
              : `The agent needs ${questions.length} answers before it can carry on.`}
          </p>

          {questions.map((question, index) => (
            <div key={index} className="flex flex-col gap-2">
              <div className="flex flex-wrap items-baseline gap-x-2">
                {question.header && (
                  <span className="rounded-md bg-muted px-1.5 py-0.5 text-micro font-medium text-muted-foreground">
                    {question.header}
                  </span>
                )}
                <p className="text-body font-medium">{question.question}</p>
              </div>

              {question.options.length > 0 && (
                <div className="flex flex-col gap-1.5">
                  {question.options.map((option) => {
                    const chosen = (picked[index] ?? []).includes(option.label);
                    return (
                      <button
                        key={option.label}
                        type="button"
                        aria-pressed={chosen}
                        disabled={sending}
                        onClick={() => toggle(index, question, option.label)}
                        className={cn(
                          "flex flex-col items-start gap-0.5 rounded-lg border px-3 py-2 text-left transition-colors",
                          chosen
                            ? "border-primary bg-primary/10"
                            : "border-border hover:bg-surface/60"
                        )}
                      >
                        <span className="text-body font-medium">{option.label}</span>
                        {option.description && (
                          <span className="text-caption text-muted-foreground">
                            {option.description}
                          </span>
                        )}
                      </button>
                    );
                  })}
                  {question.multi_select && (
                    <p className="text-micro text-muted-foreground">Pick as many as apply.</p>
                  )}
                </div>
              )}

              <Input
                value={typed[index] ?? ""}
                disabled={sending}
                placeholder={
                  question.options.length > 0
                    ? "Or answer in your own words"
                    : "Type your answer"
                }
                onChange={(event) =>
                  setTyped((current) => ({ ...current, [index]: event.target.value }))
                }
                onKeyDown={(event) => {
                  if (event.key === "Enter" && answered && !sending) void send();
                }}
              />
            </div>
          ))}

          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" disabled={!answered || sending} onClick={() => void send()}>
              <SendIcon />
              {sending ? "Sending" : "Send"}
            </Button>
            <span className="text-micro text-muted-foreground">
              The conversation is stopped until you answer.
            </span>
          </div>

          {error !== null && <p className="text-caption text-status-failed">{error}</p>}
        </div>
      </div>
    </div>
  );
}

/**
 * One free-text question, for a Run suspended before questions were
 * structured.
 *
 * `Suspended.questions` defaults to empty, so a Run that parked under an older
 * build has only the one-line `summary`. Rendering nothing would leave that
 * conversation stuck with no way to answer it.
 */
function fallback(pending: PendingQuestion): AskedQuestion[] {
  return [
    {
      question: pending.summary || "The agent asked something.",
      header: "",
      options: [],
      multi_select: false,
    },
  ];
}
