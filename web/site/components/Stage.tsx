"use client";

import { useEffect, useRef } from "react";

/**
 * A stage: the wrapper that clips the decorative layer, stacks real content
 * above it, and leans that decoration toward the cursor.
 *
 * It writes two custom properties on its own element, `--px` and `--py`, each
 * from -0.5 to 0.5 with 0 at the centre. Anything inside can lean toward the
 * pointer with a `translate` of its own, which is how the blooms and the glass
 * plates get their parallax without any of them knowing about the mouse.
 *
 * Three things keep it cheap and polite:
 *
 * - The properties land on this element, never on `:root`. A custom property
 *   set on the document invalidates style for everything that inherits it, on
 *   every single mouse move.
 * - Writes are batched into one animation frame, so a mouse firing 200 events a
 *   second still causes at most one write per frame.
 * - It does nothing for a coarse pointer or when the reader has asked for
 *   reduced motion. A finger cannot hover, and cursor parallax is exactly what
 *   that setting exists to switch off.
 *
 * Elements marked `data-sheen` also get `--mx` and `--my` as percentages of
 * their own box, for the highlight that follows the cursor across a card.
 */
export function Stage({
  children,
  className,
  reactive = true,
  ...rest
}: {
  children: React.ReactNode;
  className?: string;
  reactive?: boolean;
} & React.HTMLAttributes<HTMLDivElement>) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el || !reactive) return;
    if (!window.matchMedia("(hover: hover) and (pointer: fine)").matches) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

    let frame = 0;
    let pending: { x: number; y: number; target: Element | null } | null = null;

    const flush = () => {
      frame = 0;
      const next = pending;
      pending = null;
      if (!next) return;
      const box = el.getBoundingClientRect();
      el.style.setProperty("--px", ((next.x - box.left) / box.width - 0.5).toFixed(3));
      el.style.setProperty("--py", ((next.y - box.top) / box.height - 0.5).toFixed(3));

      const card = next.target?.closest<HTMLElement>("[data-sheen]");
      if (card) {
        const c = card.getBoundingClientRect();
        card.style.setProperty("--mx", `${(((next.x - c.left) / c.width) * 100).toFixed(1)}%`);
        card.style.setProperty("--my", `${(((next.y - c.top) / c.height) * 100).toFixed(1)}%`);
      }
    };

    const onMove = (e: PointerEvent) => {
      pending = { x: e.clientX, y: e.clientY, target: e.target as Element | null };
      if (!frame) frame = requestAnimationFrame(flush);
    };
    const onLeave = () => {
      el.style.setProperty("--px", "0");
      el.style.setProperty("--py", "0");
    };

    el.addEventListener("pointermove", onMove, { passive: true });
    el.addEventListener("pointerleave", onLeave);
    return () => {
      el.removeEventListener("pointermove", onMove);
      el.removeEventListener("pointerleave", onLeave);
      if (frame) cancelAnimationFrame(frame);
    };
  }, [reactive]);

  return (
    <div ref={ref} className={`stage${className ? ` ${className}` : ""}`} {...rest}>
      {children}
    </div>
  );
}
