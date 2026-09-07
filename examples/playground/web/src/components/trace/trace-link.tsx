import Link from "next/link";
import { ActivityIcon } from "lucide-react";

/**
 * The entry point into a Run's trace, rendered in `RunHeader` beside the
 * usage popover. It leaves Chat for Activity, which is where the technical
 * view lives: the trace used to sit under `/chat/[runId]/trace`, which put
 * run ids, hashes and sequence numbers one click inside the surface that is
 * meant to have none of them. Both answer the same question -- what did this Run actually
 * do -- so they belong next to each other rather than one in the header and
 * one floating above the composer, where it sat over the message box.
 *
 * (It was a floating control originally only because `RunHeader` was being
 * edited concurrently at the time. That is no longer true.)
 */
export function TraceLink({ runId }: { runId: string }) {
  return (
    <Link
      href={`/activity/${runId}/trace`}
      className="flex items-center gap-1.5 rounded-md px-2 py-1 text-caption font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
    >
      <ActivityIcon className="size-3.5 text-primary" />
      Trace
    </Link>
  );
}
