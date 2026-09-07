"use client";

import { useEffect, useRef } from "react";

/**
 * A vertical rail the Run's state travels down as the reader scrolls.
 *
 * The stages are server-rendered children; this only decides which one is
 * nearest the middle of the viewport and marks it, so the copper marker on the
 * rail and the highlighted stage agree. With reduced motion, or without
 * JavaScript, every stage is fully readable and the rail is a static line.
 */
export function LifecycleRail({ children }: { children: React.ReactNode }) {
  const ref = useRef<HTMLOListElement>(null);

  useEffect(() => {
    const root = ref.current;
    if (!root || !("IntersectionObserver" in window)) return;
    const stages = Array.from(root.querySelectorAll<HTMLElement>("[data-stage]"));
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          entry.target.classList.toggle("active", entry.isIntersecting);
        }
      },
      { rootMargin: "-45% 0px -45% 0px", threshold: 0 },
    );
    stages.forEach((s) => observer.observe(s));
    return () => observer.disconnect();
  }, []);

  return (
    <ol className="rail" ref={ref}>
      {children}
    </ol>
  );
}
