import { JsonBlock } from "@/components/chat/json-block";

/**
 * A tool call's arguments, for a reader rather than for a debugger.
 *
 * Most arguments are one scalar each -- a path, a query, a row count -- and
 * dumping `{"query": "orders since May"}` as JSON makes a person parse
 * punctuation to find one string they could have read directly. Scalars get a
 * label and their value; anything genuinely structured falls back to JSON,
 * which is the honest rendering for a nested object.
 */
export function ToolArguments({ value }: { value: Record<string, unknown> }) {
  const entries = Object.entries(value);
  if (entries.length === 0) {
    return <p className="text-caption text-muted-foreground italic">No arguments.</p>;
  }

  return (
    <dl className="flex flex-col gap-1.5">
      {entries.map(([key, item]) => (
        <div key={key} className="flex flex-col gap-0.5 sm:flex-row sm:gap-3">
          <dt className="shrink-0 text-caption text-muted-foreground sm:w-32 sm:text-right">
            {humanise(key)}
          </dt>
          <dd className="min-w-0 flex-1 text-caption break-words">
            {isScalar(item) ? (
              <span className="whitespace-pre-wrap">{scalarText(item)}</span>
            ) : (
              <JsonBlock value={item} />
            )}
          </dd>
        </div>
      ))}
    </dl>
  );
}

function isScalar(value: unknown): boolean {
  return (
    typeof value === "string" ||
    typeof value === "number" ||
    typeof value === "boolean" ||
    value === null
  );
}

function scalarText(value: unknown): string {
  if (value === null) return "none";
  if (typeof value === "boolean") return value ? "yes" : "no";
  return String(value);
}

/** `max_rows` reads as "Max rows". Tool authors name arguments for a schema,
 * and a schema's casing does not belong in a sentence a person reads. */
function humanise(key: string): string {
  const spaced = key.replace(/[_-]+/g, " ").replace(/([a-z\d])([A-Z])/g, "$1 $2").trim();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}
