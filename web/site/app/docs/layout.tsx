"use client";

import type { ReactNode } from "react";
import { lazy } from "react";
import { RootProvider } from "fumadocs-ui/provider/next";

/**
 * Everything fumadocs needs, scoped to the documentation.
 *
 * `DocsLayout` itself is rendered by the page rather than here, because it
 * needs the tree for the version being read and a layout in a catch-all route
 * cannot see the slug that says which version that is. See
 * `app/docs/[[...slug]]/page.tsx`.
 *
 * `theme.enabled: false` defers to the provider in the root layout. Two
 * next-themes providers on one page fight over the same storage key and the
 * same class on `<html>`.
 *
 * The dialog is ours (`components/DocsSearch.tsx`): it reads the same exported
 * static index, which is what makes search work on a site with no server, and
 * groups the matches by page instead of listing every indexed paragraph. It is
 * lazy so it costs nothing until somebody presses the key. Passing a component
 * to a client provider is what makes this file a client component; it does no
 * server work, so that costs nothing either.
 */
const DocsSearch = lazy(() => import("@/components/DocsSearch"));

export default function Layout({ children }: { children: ReactNode }) {
  return (
    <RootProvider theme={{ enabled: false }} search={{ SearchDialog: DocsSearch }}>
      {children}
    </RootProvider>
  );
}
