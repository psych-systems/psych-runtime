"use client";

import { Section } from "@/components/ui/page";
import { ThemeToggle } from "@/components/app-shell/theme-toggle";

/**
 * The theme, where someone looking for a setting would look for it.
 *
 * This is the same control the sidebar footer carries, not a second one: both
 * read and write `next-themes`, so whichever gets used the other follows. The
 * sidebar keeps its copy because that is where it is already muscle memory.
 */
export function AppearanceSection() {
  return (
    <Section title="Appearance" description="How this app looks on this device.">
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-border bg-card px-4 py-3">
        <div className="flex flex-col gap-0.5">
          <span className="text-body font-medium">Theme</span>
          <span className="text-caption text-muted-foreground">
            Following your system is the default. It applies to this browser only.
          </span>
        </div>
        <div className="w-36 rounded-lg border border-border">
          <ThemeToggle />
        </div>
      </div>
    </Section>
  );
}
