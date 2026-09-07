import { ApiError, BackendUnreachableError } from "@/lib/api";

/** One human-readable line for any error this client throws -- used
 * wherever a hook surfaces a failed request without wanting to duplicate
 * the `instanceof` chain at every call site. */
export function describeApiError(err: unknown): string {
  if (err instanceof BackendUnreachableError) return err.message;
  if (err instanceof ApiError) {
    if (err.issues.length > 0) {
      const first = err.issues[0];
      return `${err.message} (${first.path}: ${first.message})`;
    }
    return err.message;
  }
  if (err instanceof DOMException && err.name === "AbortError") return "cancelled";
  if (err instanceof Error) return err.message;
  return readableUnknown(err);
}

function readableUnknown(value: unknown): string {
  if (typeof value === "string") return value;
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    if (typeof record.message === "string") return record.message;
    if (typeof record.detail === "string") return record.detail;
    if (Array.isArray(record.detail)) {
      const lines = record.detail.map(validationLine).filter((line): line is string => line !== null);
      if (lines.length > 0) return lines.join("; ");
    }
    try {
      const json = JSON.stringify(value);
      if (json !== "{}") return json;
    } catch {
      // Fall through to the stable final message.
    }
  }
  return "Something went wrong.";
}

function validationLine(value: unknown): string | null {
  if (!value || typeof value !== "object") return null;
  const item = value as Record<string, unknown>;
  if (typeof item.msg !== "string") return null;
  const path = Array.isArray(item.loc)
    ? item.loc.filter((part) => part !== "body").map(String).join(".")
    : "";
  return path ? `${path}: ${item.msg}` : item.msg;
}
