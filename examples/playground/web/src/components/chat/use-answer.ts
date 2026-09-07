"use client";

import { useEffect, useState } from "react";

import { getRunAnswer } from "@/lib/api";
import type { AnswerView } from "@/lib/types";

/**
 * The library's own answer for a Run, read once that Run has settled.
 *
 * ## Why this is a confirmation and not the live view
 *
 * There were two ways to build this screen. Poll the answer endpoint the whole
 * time, or render the Records that are already arriving and read the endpoint
 * when the Run lands. This is the second, for three reasons.
 *
 * Polling cannot stream. The answer endpoint gives a Run's conclusion, and a
 * conclusion only exists once the Run has one, so a polled chat sits on a
 * spinner for the whole of a four-turn run and then drops the finished text in
 * at once. The Records are already coming down an open connection, turn by
 * turn and tool call by tool call, which is what lets this surface say what is
 * happening now instead of that it is busy.
 *
 * Polling would also cost a request per second per open conversation for
 * nothing: a conversation is a chain of Runs, so a ten turn thread would poll
 * ten endpoints to rebuild what the logs on screen already say.
 *
 * And the swap has to be invisible. It is, because `exchange.ts` applies the
 * library's own rule -- the answer is the turn the loop finished on -- to the
 * same Records the endpoint reads. The two agree, so nothing on screen moves
 * when this lands; what it buys is that if they ever disagree, the library
 * wins rather than this file's port of its rule. That is the whole reason to
 * spend one request per settled Run.
 *
 * Only the Run at the end of the chain is asked. Earlier Runs in a
 * conversation are settled history whose whole logs have already been read,
 * and asking for each of them would trade the flicker this avoids for ten
 * requests on opening a conversation.
 */
export function useRunAnswer(runId: string | null, settled: boolean): AnswerView | null {
  const [answer, setAnswer] = useState<AnswerView | null>(null);

  // Cleared during render rather than in an effect: a Run whose id just
  // changed must never show the previous conversation's answer, not even for
  // the one frame before the fetch below replaces it.
  const [prevRunId, setPrevRunId] = useState(runId);
  if (runId !== prevRunId) {
    setPrevRunId(runId);
    setAnswer(null);
  }

  useEffect(() => {
    if (runId === null || !settled) return;
    const controller = new AbortController();
    // Deferred a tick, as everywhere else here: this is the effect reacting to
    // the Run settling, not work owed to the commit that noticed it.
    const kickoff = setTimeout(() => {
      void (async () => {
        try {
          const view = await getRunAnswer(runId, controller.signal);
          if (!controller.signal.aborted) setAnswer(view);
        } catch {
          // The Records on screen are already the answer. A failed
          // confirmation is not worth an error a reader has to dismiss.
        }
      })();
    }, 0);
    return () => {
      controller.abort();
      clearTimeout(kickoff);
    };
  }, [runId, settled]);

  return answer;
}
