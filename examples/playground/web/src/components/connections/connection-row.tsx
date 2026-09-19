"use client";

import { useState } from "react";
import {
  ChevronRightIcon,
  KeyRoundIcon,
  Loader2Icon,
  PencilIcon,
  PlugIcon,
  PlugZapIcon,
  RefreshCwIcon,
  ShieldCheckIcon,
  Trash2Icon,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DetailRow, TechnicalDetails } from "@/components/ui/page";
import { TableCell, TableRow } from "@/components/ui/table";
import { formatClockTime, formatDayLabel } from "@/lib/format";
import { cn } from "@/lib/utils";
import { ToolCatalogue } from "@/components/connections/tool-catalogue";
import {
  describeConnection,
  describedAs,
  preloadSummary,
} from "@/components/connections/connection-state";
import type { McpServerPreset } from "@/components/settings/types";
import type { McpTestResult } from "@/lib/types";

interface ConnectionRowProps {
  connection: McpServerPreset;
  /** The result of a test run on this page, when the attempt could not be
   *  made at all and so left no stored record behind. */
  attempt: McpTestResult | undefined;
  testing: boolean;
  onTest: () => void;
  /** Drop the connection and its token, then connect again. */
  onReconnect: () => void;
  /** Close the connection and forget its token, leaving the preset. */
  onDisconnect: () => void;
  onEdit: () => void;
  onDelete: () => void;
}

/** The address as a person recognises it, with the scheme and path out of the
 *  way. The full address is one disclosure below. */
function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

function whenChecked(iso: string): string {
  return `${formatDayLabel(iso)} at ${formatClockTime(iso)}`;
}

function catalogueAge(seconds: number | null): string {
  if (seconds === null) return "not read yet";
  if (seconds < 60) return `${Math.round(seconds)}s old`;
  return `${Math.round(seconds / 60)}m old`;
}

/**
 * One connection, as two table rows: the line you scan, and the detail you
 * open when something is wrong.
 *
 * The status comes from the backend's stored record rather than from whether
 * anybody happens to have clicked something this minute, so a server that
 * answered with 351 tools still says so after a reload.
 */
export function ConnectionRow({
  connection,
  attempt,
  testing,
  onTest,
  onReconnect,
  onDisconnect,
  onEdit,
  onDelete,
}: ConnectionRowProps) {
  const [open, setOpen] = useState(false);
  const state = describeConnection(connection);
  const tools = connection.last_connection?.tools ?? [];
  const description = describedAs(connection);

  const dot =
    state.health === "connected"
      ? "bg-status-done"
      : state.health === "failed"
        ? "bg-status-failed"
        : "bg-muted-foreground/50";

  return (
    <>
      <TableRow className="align-top">
        <TableCell>
          <span className="flex min-w-0 items-start gap-2">
            <button
              type="button"
              onClick={() => setOpen((value) => !value)}
              aria-expanded={open}
              aria-label={open ? `Hide ${connection.name} detail` : `Show ${connection.name} detail`}
              className="mt-0.5 rounded text-muted-foreground transition-colors hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none"
            >
              <ChevronRightIcon
                className={cn("size-4 transition-transform", open && "rotate-90")}
                aria-hidden
              />
            </button>
            <span className="flex min-w-0 flex-col gap-0.5">
              <span className="flex flex-wrap items-center gap-1.5">
                <span className="font-medium">{connection.name}</span>
                {connection.optional && (
                  <Badge variant="outline" className="text-muted-foreground">
                    Optional
                  </Badge>
                )}
              </span>
              <span className="truncate text-caption text-muted-foreground md:hidden">
                {hostOf(connection.url)}
              </span>
            </span>
          </span>
        </TableCell>

        <TableCell className="hidden text-caption text-muted-foreground sm:table-cell">
          <span className="flex flex-col gap-0.5">
            <span>{connection.transport === "sse" ? "SSE" : "HTTP"}</span>
            {connection.oauth ? (
              <span className="inline-flex items-center gap-1">
                <ShieldCheckIcon className="size-3" aria-hidden />
                {connection.oauth.grant === "authorization_code" ? "Browser" : "App"}
              </span>
            ) : connection.credential ? (
              <span className="inline-flex items-center gap-1">
                <KeyRoundIcon className="size-3" aria-hidden />
                Token
              </span>
            ) : (
              <span>No sign-in</span>
            )}
          </span>
        </TableCell>

        <TableCell className="hidden max-w-56 truncate font-technical text-caption text-muted-foreground md:table-cell">
          {hostOf(connection.url)}
        </TableCell>

        <TableCell>
          <span className="flex min-w-0 flex-col gap-0.5">
            <span className="inline-flex items-center gap-1.5 text-caption">
              <span
                className={cn("size-2 shrink-0 rounded-full", dot, testing && "animate-pulse")}
                aria-hidden
              />
              <span
                className={cn(
                  "font-medium",
                  state.health === "failed" && "text-status-failed",
                  state.health === "untested" && "text-muted-foreground",
                )}
              >
                {testing ? "Connecting now" : state.headline}
              </span>
            </span>
            {state.checkedAt && !testing && (
              <span className="text-micro text-muted-foreground">
                checked {whenChecked(state.checkedAt)}
              </span>
            )}
            {/* The server's own words. A credential that does not exist reads
                nothing like an address that answers with a login page. */}
            {state.health === "failed" && state.detail && (
              <span className="max-w-64 text-micro break-words text-muted-foreground">
                {state.detail}
              </span>
            )}
            {/* Shown only when the attempt itself could not be made, so there
                is no stored record to read the status from. */}
            {attempt && !attempt.ok && state.health !== "failed" && (
              <span className="max-w-64 text-micro break-words text-status-failed">
                {attempt.detail}
              </span>
            )}
          </span>
        </TableCell>

        <TableCell className="tabular hidden text-caption text-muted-foreground lg:table-cell">
          {state.toolCount ?? "--"}
        </TableCell>

        <TableCell>
          <span className="flex flex-wrap items-center justify-end gap-1">
            {/* Connect and Disconnect are a pair, and only one of them makes
                sense at a time. A connected server previously offered only
                "Test connection" again, so there was no way to close one and
                no way to say "throw the token away and start over". */}
            {state.health === "connected" ? (
              <>
                <Button size="xs" variant="outline" disabled={testing} onClick={onReconnect}>
                  {testing ? <Loader2Icon className="animate-spin" /> : <RefreshCwIcon />}
                  {testing ? "Reconnecting" : "Reconnect"}
                </Button>
                <Button size="xs" variant="ghost" disabled={testing} onClick={onDisconnect}>
                  <PlugIcon /> Disconnect
                </Button>
              </>
            ) : (
              <Button size="xs" variant="outline" disabled={testing} onClick={onTest}>
                {testing ? <Loader2Icon className="animate-spin" /> : <PlugZapIcon />}
                {testing ? "Connecting" : "Test"}
              </Button>
            )}
            <Button
              size="icon-xs"
              variant="ghost"
              aria-label={`Edit ${connection.name}`}
              onClick={onEdit}
            >
              <PencilIcon />
            </Button>
            <Button
              size="icon-xs"
              variant="ghost"
              aria-label={`Remove ${connection.name}`}
              onClick={onDelete}
            >
              <Trash2Icon />
            </Button>
          </span>
        </TableCell>
      </TableRow>

      {open && (
        <TableRow className="hover:bg-transparent">
          <TableCell colSpan={6} className="bg-surface/30">
            <div className="flex flex-col gap-3 py-1">
              {/* What the agent is told this system is for. The only place
                  anyone can see what a model reads before it picks between
                  three connections whose names mean nothing to it. */}
              {description === "" ? (
                <p className="text-caption text-muted-foreground">
                  Nobody has said what this is for, so an agent choosing between connections has
                  only the name.{" "}
                  <button
                    type="button"
                    onClick={onEdit}
                    className="font-medium text-primary underline underline-offset-2"
                  >
                    Describe it
                  </button>
                  .
                </p>
              ) : (
                <p className="text-body text-surface-foreground">{description}</p>
              )}

              <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-caption text-muted-foreground">
                <span>
                  Tools in the prompt:{" "}
                  <span className="text-foreground">{preloadSummary(connection.preload)}</span>
                </span>
                <span>
                  Allowed:{" "}
                  <span className="text-foreground">
                    {connection.allow.length === 0
                      ? "everything it offers"
                      : `${connection.allow.length} chosen`}
                  </span>
                </span>
                {connection.credential && (
                  <span className="inline-flex items-center gap-1">
                    <KeyRoundIcon className="size-3" aria-hidden /> {connection.credential}
                  </span>
                )}
              </div>

              {state.health === "untested" && (
                <p className="text-caption text-muted-foreground">
                  Nothing has been tried yet. Test it to find out what it offers.
                </p>
              )}

              {state.health === "connected" && tools.length > 0 && <ToolCatalogue tools={tools} />}

              <TechnicalDetails>
                <DetailRow label="Address">
                  <span className="font-technical">{connection.url}</span>
                </DetailRow>
                <DetailRow label="Transport">
                  <span className="font-technical">{connection.transport}</span>
                </DetailRow>
                {connection.allow.length > 0 && (
                  <DetailRow label="Allow-list">
                    <span className="font-technical">{connection.allow.join(", ")}</span>
                  </DetailRow>
                )}
                {connection.credential && (
                  <DetailRow label="Credential name">
                    <span className="font-technical">{connection.credential}</span>
                  </DetailRow>
                )}
                {connection.oauth && (
                  <>
                    <DetailRow label="OAuth grant">
                      <span className="font-technical">{connection.oauth.grant}</span>
                    </DetailRow>
                    <DetailRow label="Client id">
                      <span className="font-technical">
                        {connection.oauth.preregistered_client_id ?? "registered on demand"}
                      </span>
                    </DetailRow>
                    <DetailRow label="Client secret name">
                      <span className="font-technical">
                        {connection.oauth.client_secret_credential ?? "none"}
                      </span>
                    </DetailRow>
                    {connection.oauth.issuer && (
                      <DetailRow label="Authorization server">
                        <span className="font-technical">{connection.oauth.issuer}</span>
                      </DetailRow>
                    )}
                  </>
                )}
                {connection.last_connection?.error_type && (
                  <DetailRow label="Failure">
                    <span className="font-technical">{connection.last_connection.error_type}</span>
                  </DetailRow>
                )}
                {/* The pooled connection this backend process is holding right
                    now, a different question from whether the last attempt
                    worked and only ever interesting to whoever runs it. */}
                {connection.live ? (
                  <>
                    <DetailRow label="Held open for">
                      <span className="font-technical">{connection.live.tenant}</span>
                    </DetailRow>
                    <DetailRow label="Pooled tools">
                      <span className="font-technical">{connection.live.tool_count}</span>
                    </DetailRow>
                    <DetailRow label="Catalogue">
                      <span className="font-technical">
                        {catalogueAge(connection.live.catalogue_age_seconds)}, refreshed every{" "}
                        {connection.live.catalogue_ttl_seconds}s
                      </span>
                    </DetailRow>
                    {connection.live.era && (
                      <DetailRow label="Era">
                        <span className="font-technical">{connection.live.era}</span>
                      </DetailRow>
                    )}
                  </>
                ) : (
                  <DetailRow label="Pooled now">no open connection in this process</DetailRow>
                )}
              </TechnicalDetails>
            </div>
          </TableCell>
        </TableRow>
      )}
    </>
  );
}
