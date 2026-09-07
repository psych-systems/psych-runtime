/**
 * What an agent can use, said in one line of chips.
 *
 * A tool name is an identifier the model actually calls, so it keeps the
 * technical face. A connection name is something a person typed into
 * Connections, so it does not. The old list rendered both as monospace
 * badges and a count badge beside them, which made a card of four tools look
 * like a build log.
 */

import { PlugIcon, WrenchIcon } from "lucide-react";

import { cn } from "@/lib/utils";

function Chip({
  children,
  mono,
  icon: Icon,
}: {
  children: string;
  mono?: boolean;
  icon: typeof WrenchIcon;
}) {
  return (
    <span className="inline-flex max-w-56 items-center gap-1.5 rounded-full bg-surface px-2.5 py-1 text-caption text-surface-foreground">
      <Icon className="size-3 shrink-0 text-muted-foreground" aria-hidden />
      <span className={cn("truncate", mono && "font-technical")}>{children}</span>
    </span>
  );
}

/**
 * `limit` keeps a card's chip row to one or two lines; the overflow is
 * counted rather than dropped, because "and three more" is a different fact
 * from "that's all of them".
 */
export function CapabilityChips({
  tools,
  connections,
  limit,
  className,
}: {
  tools: readonly string[];
  connections: readonly string[];
  limit?: number;
  className?: string;
}) {
  const total = tools.length + connections.length;
  if (total === 0) {
    return (
      <p className={cn("text-caption text-muted-foreground", className)}>
        Answers on its own, with nothing else to call.
      </p>
    );
  }

  const cap = limit ?? total;
  const shownTools = tools.slice(0, cap);
  const shownConnections = connections.slice(0, Math.max(0, cap - shownTools.length));
  const hidden = total - shownTools.length - shownConnections.length;

  return (
    <div className={cn("flex flex-wrap items-center gap-1.5", className)}>
      {shownTools.map((tool) => (
        <Chip key={`tool-${tool}`} icon={WrenchIcon} mono>
          {tool}
        </Chip>
      ))}
      {shownConnections.map((connection) => (
        <Chip key={`mcp-${connection}`} icon={PlugIcon}>
          {connection}
        </Chip>
      ))}
      {hidden > 0 && (
        <span className="text-caption text-muted-foreground">and {hidden} more</span>
      )}
    </div>
  );
}
