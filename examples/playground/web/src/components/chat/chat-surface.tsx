"use client";

import type { ReactNode } from "react";
import { usePathname } from "next/navigation";

import { ChatWorkspace } from "@/components/chat/chat-workspace";

const CONVERSATION = /^\/chat\/([^/]+)$/;

/**
 * What the `(chat)` layout renders, and why the layout renders anything at
 * all.
 *
 * `/chat` and `/chat/{id}` were two page components, so dispatching a message
 * unmounted the first and mounted the second: the Run that had just been
 * dispatched, the message on screen and the open stream were all discarded,
 * and the new page re-fetched what the old one already had. Keeping the
 * workspace in the layout makes that navigation what it looks like -- the same
 * conversation, now with an address.
 *
 * The Run comes from the path rather than from a page's params for the same
 * reason: a value read here changes without anything being recreated.
 */
export function ChatSurface({ children }: { children: ReactNode }) {
  const pathname = usePathname();

  // Anything deeper than /chat/{id} is a different view of a Run (its trace),
  // not the conversation, and owns the whole surface.
  if (pathname.startsWith("/chat/") && !CONVERSATION.test(pathname)) {
    return <>{children}</>;
  }

  const match = CONVERSATION.exec(pathname);
  return <ChatWorkspace routeRunId={match ? decodeURIComponent(match[1]) : null} />;
}
