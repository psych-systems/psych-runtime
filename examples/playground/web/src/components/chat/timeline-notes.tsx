import { InfoIcon, OctagonXIcon, TriangleAlertIcon } from "lucide-react";

import type { EndedItem, NoteItem } from "@/lib/conversation";

/**
 * How a conversation ended, when the ending is worth a line.
 *
 * A completed Run gets nothing: the answer above it is the ending. A failure
 * gets the message the library already wrote for a person -- never a
 * traceback, which belongs to whoever operates the platform and is on the
 * trace for them.
 */
export function EndedNote({
  item,
  failureMessage,
}: {
  item: EndedItem;
  /** `RunStatus.failure_message`, preferred over the log's own copy when the
   * status has been read: it is composed through the library's guidance
   * module and says what to do next, not just what broke. */
  failureMessage?: string | null;
}) {
  const failed = item.state === "failed" || item.state === "force_settled";
  const message = failureMessage ?? item.failureMessage;

  if (!failed) {
    return (
      <Note
        icon={OctagonXIcon}
        text={
          item.state === "abandoned"
            ? "This answer was abandoned before it finished."
            : "Stopped before it finished."
        }
      />
    );
  }

  return (
    <div className="flex w-full gap-2.5 rounded-lg border border-status-failed/30 bg-status-failed/5 px-3 py-2.5 text-body text-foreground">
      <TriangleAlertIcon className="mt-0.5 size-4 shrink-0 text-status-failed" />
      <div className="flex min-w-0 flex-col gap-1">
        <p className="font-medium text-status-failed">This did not finish.</p>
        <p className="text-muted-foreground">
          {message ?? "The run failed and left no explanation."}
        </p>
      </div>
    </div>
  );
}

export function TimelineNote({ item }: { item: NoteItem }) {
  return <Note icon={InfoIcon} text={item.text} />;
}

function Note({
  icon: Icon,
  text,
}: {
  icon: typeof InfoIcon;
  text: string;
}) {
  return (
    <p className="flex w-full items-center gap-2 text-caption text-muted-foreground">
      <Icon className="size-3.5 shrink-0" aria-hidden />
      {text}
    </p>
  );
}
