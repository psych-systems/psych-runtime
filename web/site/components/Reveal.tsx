"use client";

import { useEffect } from "react";

/**
 * Marks decorated elements as in view, once, with one observer for the page.
 *
 * Rendered once per page rather than wrapping each element, so the markup stays
 * server-rendered HTML with a class on it. Three effects share the observer:
 * `.reveal` fades, `.rise` lifts, `.sweep` draws a line. Each keeps its own
 * timing in CSS, and `prefers-reduced-motion` disables all three there.
 *
 * Children of a `.stagger` container are numbered as they are observed, so a
 * row of tiles arrives in order without every page hand-writing delays.
 */
export function Reveal() {
  useEffect(() => {
    const targets = document.querySelectorAll<HTMLElement>(".reveal, .rise, .sweep");
    if (targets.length === 0) return;

    if (!("IntersectionObserver" in window)) {
      targets.forEach((el) => el.classList.add("in"));
      return;
    }

    for (const group of document.querySelectorAll<HTMLElement>(".stagger")) {
      Array.from(group.children).forEach((child, i) => {
        (child as HTMLElement).style.setProperty("--d", String(i));
      });
    }

    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue;
          entry.target.classList.add("in");
          observer.unobserve(entry.target);
        }
      },
      { threshold: 0.06, rootMargin: "0px 0px -8% 0px" },
    );
    targets.forEach((el) => observer.observe(el));
    return () => observer.disconnect();
  }, []);

  return null;
}
