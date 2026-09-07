"use client";

import { useEffect, useState } from "react";
import { useTheme } from "next-themes";
import { Moon, Sun } from "lucide-react";
import { ICON } from "@/components/icons";

/**
 * Light and dark, on one button.
 *
 * It renders a fixed placeholder until mounted. The server cannot know which
 * theme the browser will resolve, so rendering the real icon on the first pass
 * guarantees a hydration mismatch and a flicker on every load.
 */
export function ThemeToggle() {
  const { resolvedTheme, setTheme } = useTheme();
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  const dark = resolvedTheme === "dark";
  return (
    <button
      type="button"
      className="icon-btn"
      onClick={() => setTheme(dark ? "light" : "dark")}
      aria-label={mounted ? `Switch to ${dark ? "light" : "dark"} theme` : "Switch theme"}
    >
      {mounted && dark ? <Moon size={ICON} aria-hidden /> : <Sun size={ICON} aria-hidden />}
    </button>
  );
}
