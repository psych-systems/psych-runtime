import "./global.css";
import type { ReactNode } from "react";
import type { Metadata, Viewport } from "next";
import { Fraunces, IBM_Plex_Mono, IBM_Plex_Sans } from "next/font/google";
import { ThemeProvider } from "next-themes";
import { PreviewBar } from "@/components/PreviewBar";
import { IS_PREVIEW } from "@/lib/env";
import { SOCIAL_IMAGE } from "@/lib/seo";
import { SITE } from "@/lib/site";

/*
 * The three faces are downloaded at build time and served from this origin.
 * That removes a render-blocking request to a third party, lets the
 * Content-Security-Policy drop Google's hosts entirely, and means a font
 * cannot change under the site between two builds.
 */
/*
 * Italic only, and no optical-size axis.
 *
 * The display face has exactly one job on this site: the `.serif` phrase in a
 * heading, which is always italic at one weight. Requesting the roman as well
 * shipped a second variable font that nothing ever rendered, and the `opsz`
 * axis carries data for sizes this site never sets.
 */
const fraunces = Fraunces({
  subsets: ["latin"],
  style: ["italic"],
  weight: ["400"],
  variable: "--font-fraunces",
  display: "swap",
});
const plexSans = IBM_Plex_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-plex-sans",
  display: "swap",
});
const plexMono = IBM_Plex_Mono({
  subsets: ["latin"],
  weight: ["400", "500"],
  variable: "--font-plex-mono",
  display: "swap",
});

export const metadata: Metadata = {
  metadataBase: new URL(SITE.url),
  title: {
    default: `${SITE.name}: run AI agents inside your Python application`,
    template: `%s · ${SITE.name}`,
  },
  description: SITE.description,
  applicationName: SITE.name,
  openGraph: {
    siteName: SITE.name,
    type: "website",
    locale: "en_GB",
    images: [SOCIAL_IMAGE],
  },
  twitter: {
    card: "summary_large_image",
    images: [SOCIAL_IMAGE],
  },
  icons: {
    icon: [{ url: "/icon.svg", type: "image/svg+xml" }],
    apple: [{ url: "/apple-touch-icon.png", sizes: "180x180", type: "image/png" }],
  },
  robots: IS_PREVIEW ? { index: false, follow: false } : { index: true, follow: true },
};

export const viewport: Viewport = {
  themeColor: "#f9f8f4",
  width: "device-width",
  initialScale: 1,
};

export default function Layout({ children }: { children: ReactNode }) {
  return (
    <html
      lang="en"
      suppressHydrationWarning
      className={`${fraunces.variable} ${plexSans.variable} ${plexMono.variable}`}
    >
      <head>
        {/*
          Entrance animations start from opacity 0 and are revealed by an
          observer. Without scripting nothing would ever reveal them, so the
          hidden start state is scoped to this flag and a reader with no
          JavaScript gets the finished page instead of a blank one.
        */}
        <script
          dangerouslySetInnerHTML={{
            __html: `document.documentElement.setAttribute('data-js','on')`,
          }}
        />
      </head>
      <body>
        {/*
          Theming for the whole site, and nothing else.

          fumadocs' RootProvider also does this, and it used to sit here, but it
          carries the search dialog and the sidebar contexts with it: every
          marketing page paid for machinery only the documentation uses. It now
          lives in `app/docs/layout.tsx` with its own theme provider turned off,
          deferring to this one, so there is still exactly one.
        */}
        <ThemeProvider attribute="class" defaultTheme="system" enableSystem disableTransitionOnChange>
          <PreviewBar />
          {children}
        </ThemeProvider>
      </body>
    </html>
  );
}
