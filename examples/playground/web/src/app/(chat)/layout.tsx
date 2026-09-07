import type { ReactNode } from "react";

import { ChatSurface } from "@/components/chat/chat-surface";

/**
 * The chat surface lives here rather than in the pages under it so that
 * navigating from `/chat` to `/chat/{id}` keeps the same component mounted.
 * The pages below are routing only; `ChatSurface` reads the path.
 */
export default function ChatLayout({ children }: { children: ReactNode }) {
  return <ChatSurface>{children}</ChatSurface>;
}
