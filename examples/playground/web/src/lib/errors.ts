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
  return "unknown error";
}
