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

interface ConnectionCardProps {
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

export function ConnectionCard({
  connection,
  attempt,
  testing,
  onTest,
  onReconnect,
  onDisconnect,
  onEdit,
  onDelete,
}: ConnectionCardProps) {
  const [browsing, setBrowsing] = useState(false);
  const state = describeConnection(connection);
  const tools = connection.last_connection?.tools ?? [];
  const description = describedAs(connection);

  const dot =
    state.health === "connected"
      ? "bg-status-done"
      : state.health === "failed"
        ? "bg-status-failed"
        : "bg-muted-foreground";

  return (
    <div className="flex flex-col gap-3 rounded-xl border border-border bg-card px-4 py-3.5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-1">
          <div className="flex items-center gap-2">
            <span className="font-medium">{connection.name}</span>
            {connection.optional && (
              <Badge variant="outline" className="text-muted-foreground">
                Optional
              </Badge>
            )}
          </div>
          <span className="truncate text-caption text-muted-foreground">
            {hostOf(connection.url)}
          </span>
        </div>

        <div className="flex shrink-0 items-center gap-1">
          {/* Connect and Disconnect are a pair, and only one of them makes
              sense at a time. A connected server previously offered only
              "Test connection" again, so there was no way to close one and no
              way to say "throw the token away and start over" -- the two
              things somebody wants when a connection is live but wrong. */}
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
              {testing ? "Connecting" : "Connect"}
            </Button>
          )}
          <Button size="xs" variant="ghost" onClick={onEdit}>
            <PencilIcon /> Edit
          </Button>
          <Button
            size="icon-xs"
            variant="ghost"
            aria-label={`Remove ${connection.name}`}
            onClick={onDelete}
          >
            <Trash2Icon />
          </Button>
        </div>
      </div>

      {/* What the agent is told this system is for. Shown here because it is
          the only place anyone can see what a model reads before it picks
          between three connections whose names mean nothing to it. */}
      {description === "" ? (
        <p className="text-body text-muted-foreground">
          Nobody has said what this is for, so an agent choosing between connections has only the
          name.{" "}
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

      <div className="flex flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className={cn("size-2 shrink-0 rounded-full", dot, testing && "animate-pulse")} />
          <span
            className={cn(
              "text-body font-medium",
              state.health === "failed" && "text-status-failed",
              state.health === "untested" && "text-muted-foreground"
            )}
          >
            {testing ? "Connecting now" : state.headline}
          </span>
          {state.checkedAt && !testing && (
            <span className="text-caption text-muted-foreground">
              checked {whenChecked(state.checkedAt)}
            </span>
          )}
        </div>

        {/* The server's own words. A credential that does not exist reads
            nothing like an address that answers with a login page, and a
            generic "connection failed" hides which one happened. */}
        {state.health === "failed" && state.detail && (
          <p className="text-caption break-words text-muted-foreground">{state.detail}</p>
        )}
        {state.health === "untested" && (
          <p className="text-caption text-muted-foreground">
            Nothing has been tried yet. Test it to find out what it offers.
          </p>
        )}
      </div>

      {/* Shown only when the attempt itself could not be made, so there is no
          stored record to read the status from. */}
      {attempt && !attempt.ok && state.health !== "failed" && (
        <p className="rounded-md border border-status-failed/30 bg-status-failed/8 px-3 py-2 text-caption text-status-failed">
          {attempt.detail}
        </p>
      )}

      {state.health === "connected" && tools.length > 0 && (
        <div className="flex flex-col gap-2">
          <button
            type="button"
            onClick={() => setBrowsing((open) => !open)}
            className="flex w-fit items-center gap-1 text-caption font-medium text-muted-foreground transition-colors hover:text-foreground"
          >
            <ChevronRightIcon
              className={cn("size-3.5 transition-transform", browsing && "rotate-90")}
            />
            {browsing ? "Hide its tools" : `Browse its ${tools.length} tools`}
          </button>
          {browsing && <ToolCatalogue tools={tools} />}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-caption text-muted-foreground">
        <span>
          Tools in the prompt: <span className="text-foreground">{preloadSummary(connection.preload)}</span>
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
            <KeyRoundIcon className="size-3" /> {connection.credential}
          </span>
        )}
        {connection.oauth && (
          <span className="inline-flex items-center gap-1">
            <ShieldCheckIcon className="size-3" />
            {connection.oauth.grant === "authorization_code"
              ? "Browser sign-in"
              : "App sign-in"}
          </span>
        )}
      </div>

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
          </>
        )}
        {connection.last_connection?.error_type && (
          <DetailRow label="Failure">
            <span className="font-technical">{connection.last_connection.error_type}</span>
          </DetailRow>
        )}
        {/* The pooled connection this backend process is holding right now,
            which is a different question from whether the last attempt worked
            and is only ever interesting to whoever is running the process. */}
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
  );
}
