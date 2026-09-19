"use client";

import { MonitorIcon, MoonIcon, SunIcon } from "lucide-react";
import { useTheme } from "next-themes";

import { FeatureRow, FeatureTable } from "@/components/ui/feature-table";
import { useIsClient } from "@/hooks/use-is-client";
import { cn } from "@/lib/utils";
import { SettingsSectionBlock } from "@/components/settings/section-nav";

const OPTIONS = [
  { value: "light", label: "Light", icon: SunIcon },
  { value: "dark", label: "Dark", icon: MoonIcon },
  { value: "system", label: "System", icon: MonitorIcon },
] as const;

/**
 * The theme, where someone looking for a setting would look for it.
 *
 * A segmented control rather than the sidebar's dropdown: this is the page
 * you come to in order to change it, so the three choices are all visible and
 * one click away. It reads and writes the same `next-themes` state the
 * sidebar's copy does, so whichever gets used the other follows.
 */
export function AppearanceSection() {
  const { theme, setTheme } = useTheme();
  // Avoid a hydration mismatch: the resolved theme is unknown on the server.
  const mounted = useIsClient();
  const current = mounted ? (theme ?? "system") : null;

  return (
    <SettingsSectionBlock id="appearance" title="Appearance">
      <FeatureTable>
        <FeatureRow
          label="Theme"
          help={
            <p>
              Following your system is the default. This is a browser setting, so it applies to
              this device only.
            </p>
          }
          control={
            <div
              role="group"
              aria-label="Theme"
              className="flex items-center gap-0.5 rounded-lg border border-border bg-surface/50 p-0.5"
            >
              {OPTIONS.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  aria-pressed={current === option.value}
                  onClick={() => setTheme(option.value)}
                  className={cn(
                    "inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-caption font-medium transition-colors focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none",
                    current === option.value
                      ? "bg-background text-foreground shadow-sm"
                      : "text-muted-foreground hover:text-foreground",
                  )}
                >
                  <option.icon className="size-3.5" aria-hidden />
                  {option.label}
                </button>
              ))}
            </div>
          }
        />
      </FeatureTable>
    </SettingsSectionBlock>
  );
}
