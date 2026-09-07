"use client";

import { useEffect, useState } from "react";

/** Roughly how long the whole answer takes to appear, however long it is. A
 *  fixed characters-per-second rate reads well for a sentence and takes half a
 *  minute for a table, so the rate scales and the duration does not. */
const REVEAL_MS = 900;
/** Below this the reveal is a flicker rather than an arrival: two words appear
 *  in three frames and it reads as a rendering glitch. */
const MIN_LENGTH = 24;

/**
 * An answer appearing at a reading rate rather than all at once.
 *
 * This is a rendering decision, and it is worth saying why it is not a lie.
 * A turn's text reaches this app on one Record, written when the model call
 * finished: there is no token by token frame in the protocol, so an answer
 * genuinely does arrive in one piece. What it does not do is arrive at one
 * moment for the reader, whose eye starts at the top of a paragraph either
 * way. Revealing it over a beat gives the eye somewhere to start, and gives
 * the scroll position something continuous to follow instead of a jump the
 * height of the whole answer.
 *
 * `animate` is false for text that was already written when the conversation
 * opened. Replaying a typewriter over yesterday's conversation every time
 * someone clicks it in the history list would be theatre.
 */
export function useRevealedText(text: string, animate: boolean): string {
  const [revealed, setRevealed] = useState(() => (animate ? "" : text));

  useEffect(() => {
    if (!animate || text.length < MIN_LENGTH || prefersReducedMotion()) {
      setRevealed(text);
      return;
    }

    const started = performance.now();
    let frame = requestAnimationFrame(function step() {
      const progress = Math.min(1, (performance.now() - started) / REVEAL_MS);
      setRevealed(text.slice(0, Math.round(text.length * progress)));
      if (progress < 1) frame = requestAnimationFrame(step);
    });
    return () => cancelAnimationFrame(frame);
  }, [text, animate]);

  return revealed;
}

function prefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}
