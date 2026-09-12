"use client";

import { useCallback, useEffect, useState } from "react";
import { DownloadIcon, Loader2Icon, SearchIcon } from "lucide-react";

import { attachmentDownloadUrl, readRunAttachment } from "@/lib/api";
import { describeApiError } from "@/lib/errors";
import { formatBytes } from "@/lib/format";
import type { ReadToolOutputResult, ResultAttachment } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

const WINDOW = 40;

/**
 * Reads one recorded output in windows, the way the model does.
 *
 * Nothing here fetches the whole thing into the page: a window of lines at
 * an offset, or the lines matching a pattern, through the same reader the
 * `read_tool_output` tool uses and against the same handle. The full file
 * is a download, not a render, so a megabyte of stdout is never poured into
 * the transcript.
 */
export function AttachmentReader({
  runId,
  attachment,
}: {
  runId: string;
  attachment: ResultAttachment;
}) {
  const [offset, setOffset] = useState(0);
  const [pattern, setPattern] = useState("");
  const [applied, setApplied] = useState("");
  const [window, setWindow] = useState<ReadToolOutputResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await readRunAttachment(runId, attachment.handle, {
        offset,
        limit: WINDOW,
        pattern: applied || undefined,
      });
      setWindow(result);
    } catch (err) {
      setError(describeApiError(err));
    } finally {
      setLoading(false);
    }
  }, [runId, attachment.handle, offset, applied]);

  useEffect(() => {
    if (attachment.stored === "preview_only") return undefined;
    const id = setTimeout(() => void load(), 0);
    return () => clearTimeout(id);
  }, [load, attachment.stored]);

  if (attachment.stored === "preview_only") {
    return (
      <p className="text-caption text-muted-foreground">
        Only the preview was kept for this output ({formatBytes(attachment.observed_bytes)}{" "}
        produced). Wire a blob store, or ask the agent to keep output in full, to read it later.
      </p>
    );
  }

  const total = window?.pattern ? (window.total_matches ?? 0) : (window?.total_lines ?? 0);
  const shown = window?.returned_lines ?? 0;
  const canPrev = offset > 0;
  const canNext = window !== null && offset + shown < total;

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <form
          className="flex min-w-0 flex-1 items-center gap-1.5"
          onSubmit={(event) => {
            event.preventDefault();
            setOffset(0);
            setApplied(pattern.trim());
          }}
        >
          <Input
            aria-label={`Search ${attachment.name}`}
            placeholder="Search lines (regular expression)"
            value={pattern}
            className="h-8 min-w-0 flex-1"
            onChange={(event) => setPattern(event.target.value)}
          />
          <Button type="submit" size="sm" variant="outline" aria-label="Search">
            <SearchIcon />
          </Button>
        </form>
        <Button asChild size="sm" variant="ghost">
          <a href={attachmentDownloadUrl(runId, attachment.handle)} download>
            <DownloadIcon /> Download {formatBytes(attachment.size_bytes)}
          </a>
        </Button>
      </div>
      {error && <p className="text-caption text-status-failed">{error}</p>}
      {window?.binary && (
        <p className="text-caption text-muted-foreground">
          Binary content, {formatBytes(window.total_size_bytes)}. Download it to open it.
        </p>
      )}
      {window && !window.binary && (
        <>
          <pre className="max-h-80 max-w-full overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted p-3 font-technical text-caption">
            {window.pattern
              ? window.matches.map((m) => `${m.line_number}: ${m.text}`).join("\n") ||
                "No line matched."
              : window.content || "(empty window)"}
          </pre>
          <div className="flex items-center gap-2 text-micro text-muted-foreground">
            <span aria-live="polite">
              {window.pattern
                ? `${total.toLocaleString()} matching ${total === 1 ? "line" : "lines"}`
                : `${total.toLocaleString()} ${total === 1 ? "line" : "lines"}`}
              {shown > 0 && `, showing ${offset + 1}–${offset + shown}`}
            </span>
            <span className="ml-auto flex items-center gap-1">
              {loading && <Loader2Icon className="size-3 animate-spin" aria-hidden />}
              <Button
                size="xs"
                variant="ghost"
                disabled={!canPrev || loading}
                onClick={() => setOffset(Math.max(0, offset - WINDOW))}
              >
                Previous
              </Button>
              <Button
                size="xs"
                variant="ghost"
                disabled={!canNext || loading}
                onClick={() => setOffset(offset + WINDOW)}
              >
                Next
              </Button>
            </span>
          </div>
        </>
      )}
    </div>
  );
}
