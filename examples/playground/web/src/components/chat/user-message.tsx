import type { RefObject } from "react";

/** What the person said. Right-aligned and filled, the one visual convention
 * every chat client shares, so the eye can find its own messages without
 * reading them. */
export function UserMessage({
  text,
  /** The bubble, for whoever copies this message off the screen. */
  contentRef,
}: {
  text: string;
  contentRef?: RefObject<HTMLDivElement | null>;
}) {
  return (
    <div className="flex w-full justify-end">
      <div
        ref={contentRef}
        className="max-w-[85%] rounded-2xl rounded-tr-sm bg-primary px-3.5 py-2 text-prose whitespace-pre-wrap text-primary-foreground"
      >
        {/* The wrapping is what the class above does on this page, said again
            inline so that it survives being copied out of it: a paste target
            has none of this app's CSS, and a message typed over three lines
            would arrive there as one. */}
        <span style={{ whiteSpace: "pre-wrap" }}>{text}</span>
      </div>
    </div>
  );
}
