"use client";

import { ThemeProvider as NextThemeProvider } from "next-themes";
import type { ComponentProps } from "react";

/** Thin wrapper so `layout.tsx` doesn't import `next-themes` directly and
 * every theme default (attribute, system-preference fallback) lives in one
 * place. `attribute="class"` toggles Tailwind's `dark:` variant via `.dark`
 * on `<html>`, matching `globals.css`. */
export function ThemeProvider({ children, ...props }: ComponentProps<typeof NextThemeProvider>) {
  return (
    <NextThemeProvider
      attribute="class"
      defaultTheme="system"
      enableSystem
      disableTransitionOnChange
      {...props}
    >
      {children}
    </NextThemeProvider>
  );
}
