import { cn } from "@/lib/utils";
import { Trident } from "@/components/brand/trident";

/**
 * The agent's side of the conversation, marked once.
 *
 * Every block the agent owns starts with this glyph in the same column, so the
 * working indicator and the answer that replaces it occupy the same left edge
 * and the same first line. That is what makes the swap look like an answer
 * arriving rather than one component being exchanged for another.
 */
export function AgentMark({ className }: { className?: string }) {
  return (
    <span
      aria-hidden
      className={cn(
        "mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-lg bg-secondary font-heading text-caption font-semibold text-secondary-foreground",
        className
      )}
    >
      <Trident className="size-4" />
    </span>
  );
}
