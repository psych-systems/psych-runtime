/**
 * How an agent shapes its final answer, in one place.
 *
 * The builder asks the question and the agent pages report it, so the wording
 * lives here rather than being written twice and drifting. `null` is a real
 * choice and carries its own copy: it is the default, and it is not "no
 * answer style", it is the model's own.
 *
 * Unlike a connection's description, this one is sealed into the published
 * agent, because it changes what the model is told. Every screen that offers
 * it says so, which is why `CHANGES_THE_AGENT` is here too rather than being
 * paraphrased on each of them.
 */

import type { AnswerStyle } from "@/lib/types";

/** `null` for the model's own shape, and every value the wire accepts. */
export type AnswerStyleChoice = AnswerStyle | null;

export interface AnswerStyleCopy {
  /** The button in the builder. */
  title: string;
  /** What the agent is asked to do. */
  blurb: string;
  /** One line for a card or a detail page, written as a statement about the
   *  agent rather than as an instruction to it. */
  summary: string;
}

export const ANSWER_STYLE_COPY: {
  detailed: AnswerStyleCopy;
  concise: AnswerStyleCopy;
} = {
  detailed: {
    title: "Detailed (default)",
    blurb:
      "The agent answers however the model would. Room to explain its reasoning, and nothing asking it to be brief.",
    summary: "Answers at whatever length the model thinks the question deserves.",
  },
  concise: {
    title: "Concise",
    blurb:
      "Lead with the answer, keep it short, and use bullets or a small table when there are several facts to present.",
    summary:
      "Leads with the answer and keeps it short, using bullets or a small table for several facts.",
  },
};

export const ANSWER_STYLE_OPTIONS: ReadonlyArray<{
  value: AnswerStyleChoice;
  copy: AnswerStyleCopy;
}> = [
  { value: null, copy: ANSWER_STYLE_COPY.detailed },
  { value: "concise", copy: ANSWER_STYLE_COPY.concise },
];

export function answerStyleCopy(style: AnswerStyleChoice): AnswerStyleCopy {
  return style === "concise" ? ANSWER_STYLE_COPY.concise : ANSWER_STYLE_COPY.detailed;
}

/** Said wherever the choice is offered. Two agents differing only in this are
 *  two agents, because the model is told something different. */
export const CHANGES_THE_AGENT =
  "This is part of the agent itself, so picking it publishes a different one. Changing your mind later means publishing again.";
