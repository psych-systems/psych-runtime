"use client";

import { MonitorIcon, MoonIcon, SunIcon } from "lucide-react";
import { useTheme } from "next-themes";

import { useIsClient } from "@/hooks/use-is-client";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

const OPTIONS = [
  { value: "light", label: "Light", icon: SunIcon },
  { value: "dark", label: "Dark", icon: MoonIcon },
  { value: "system", label: "System", icon: MonitorIcon },
] as const;

export function ThemeToggle({ collapsed = false }: { collapsed?: boolean }) {
  const { theme, setTheme } = useTheme();
  // Avoid a hydration mismatch: the resolved theme is unknown on the server.
  const mounted = useIsClient();

  const current = OPTIONS.find((o) => o.value === theme) ?? OPTIONS[2];
  const Icon = current.icon;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size={collapsed ? "icon-sm" : "sm"}
          className={cn(
            "gap-2",
            // The rail's icon column, not this button's natural size:
            // `icon-sm` is 28px and every nav icon above it sits in 32px,
            // which put this one two pixels to their left.
            collapsed ? "size-8 justify-center" : "w-full justify-start",
          )}
          aria-label="Change theme"
        >
          {mounted ? <Icon className="size-3.5" /> : <span className="size-3.5" />}
          {!collapsed && <span>{mounted ? current.label : "Theme"}</span>}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-36">
        {OPTIONS.map((option) => (
          <DropdownMenuItem
            key={option.value}
            onClick={() => setTheme(option.value)}
            className="gap-2"
          >
            <option.icon className="size-3.5 text-muted-foreground" />
            {option.label}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
