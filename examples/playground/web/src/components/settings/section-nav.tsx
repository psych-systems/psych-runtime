"use client";

import { useEffect, useState } from "react";

import { cn } from "@/lib/utils";

/**
 * The sticky strip that moves between this page's sections.
 *
 * Anchors, deliberately not tabs. Radix tabs unmount what they hide, and
 * everything worth keeping here is the result of an action -- whether a key
 * worked, what a sandbox probe found -- so a tab switch would throw it away.
 * These jump; nothing unmounts, and the browser's own back button works.
 */
export interface SettingsSection {
  id: string;
  label: string;
}

export const SETTINGS_SECTIONS: SettingsSection[] = [
  { id: "providers", label: "Providers" },
  { id: "runtime", label: "Runtime" },
  { id: "sandbox", label: "Sandbox" },
  { id: "secrets", label: "Secrets" },
  { id: "skills", label: "Skills" },
  { id: "prices", label: "Prices" },
  { id: "memory", label: "Memory" },
  { id: "appearance", label: "Appearance" },
];

export function SectionNav({ sections }: { sections: SettingsSection[] }) {
  const [current, setCurrent] = useState(sections[0]?.id ?? "");

  useEffect(() => {
    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((entry) => entry.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0];
        if (visible) setCurrent(visible.target.id);
      },
      // A band across the upper third: the section whose heading has just
      // passed under the strip is the one the strip should be pointing at.
      { rootMargin: "-88px 0px -66% 0px", threshold: 0 },
    );
    for (const section of sections) {
      const element = document.getElementById(section.id);
      if (element) observer.observe(element);
    }
    return () => observer.disconnect();
  }, [sections]);

  return (
    <nav
      aria-label="Settings sections"
      // Sticks against the pane `Page` scrolls in. `-mx-4`/`px-4` lets the
      // strip itself scroll sideways at phone width without the page doing
      // so, and the backdrop keeps content legible as it passes underneath.
      className="sticky top-0 z-20 -mx-4 border-b border-border/70 bg-background/85 px-4 backdrop-blur sm:-mx-7 sm:px-7 lg:-mx-10 lg:px-10"
    >
      <ul className="flex gap-1 overflow-x-auto py-2 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
        {sections.map((section) => (
          <li key={section.id}>
            <a
              href={`#${section.id}`}
              aria-current={current === section.id ? "true" : undefined}
              className={cn(
                "inline-flex shrink-0 rounded-lg px-2.5 py-1.5 text-caption font-medium whitespace-nowrap transition-colors focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none",
                current === section.id
                  ? "bg-surface text-foreground"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              {section.label}
            </a>
          </li>
        ))}
      </ul>
    </nav>
  );
}

/**
 * A section of the settings page: the anchor the strip jumps to, a heading,
 * at most one line of prose, and its own actions.
 *
 * `scroll-mt` is what keeps the heading clear of the sticky strip after a
 * jump, rather than landing underneath it.
 */
export function SettingsSectionBlock({
  id,
  title,
  description,
  actions,
  children,
}: {
  id: string;
  title: string;
  description?: React.ReactNode;
  actions?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section id={id} className="flex scroll-mt-20 flex-col gap-3.5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-1">
          <h2 className="text-base font-semibold">{title}</h2>
          {description && (
            <p className="max-w-2xl text-caption text-muted-foreground">{description}</p>
          )}
        </div>
        {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
      </div>
      {children}
    </section>
  );
}
