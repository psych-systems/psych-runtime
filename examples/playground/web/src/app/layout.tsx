import type { Metadata } from "next";
import { Fraunces, IBM_Plex_Mono, IBM_Plex_Sans } from "next/font/google";

import { AppShell } from "@/components/app-shell/shell";
import { ThemeProvider } from "@/components/app-shell/theme-provider";
import { Toaster } from "@/components/ui/sonner";

import "./globals.css";

const bodyFont = IBM_Plex_Sans({
  variable: "--font-body",
  subsets: ["latin"],
  weight: ["400", "500", "600"],
});

const displayFont = Fraunces({
  variable: "--font-display",
  subsets: ["latin"],
  weight: "variable",
  style: ["normal", "italic"],
  axes: ["opsz", "SOFT", "WONK"],
});

const technicalFont = IBM_Plex_Mono({
  variable: "--font-technical",
  subsets: ["latin"],
  weight: ["400", "500"],
});

export const metadata: Metadata = {
  title: "Psych Playground",
  description: "A hands-on console for dispatching, watching and steering Psych agent runs.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      suppressHydrationWarning
      className={`${bodyFont.variable} ${displayFont.variable} ${technicalFont.variable} h-full antialiased`}
    >
      <body className="flex h-full min-h-svh flex-col">
        <ThemeProvider>
          <AppShell>{children}</AppShell>
          <Toaster position="bottom-right" />
        </ThemeProvider>
      </body>
    </html>
  );
}
