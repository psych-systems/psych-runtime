"use client";

import { RefreshCwIcon } from "lucide-react";

import { useBackendConfig } from "@/components/app-shell/backend-config-provider";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

/** The backend URL and a live dot, with the full picture (model, store,
 * error) on hover. Collapses to just the dot when the sidebar is
 * icon-only. */
export function HealthIndicator({ collapsed = false }: { collapsed?: boolean }) {
  const { config, status, error, refresh, baseUrl } = useBackendConfig();

  const dotClass = cn(
    "size-2 shrink-0 rounded-full",
    status === "online" && "bg-status-completed",
    status === "offline" && "bg-status-failed",
    status === "checking" && "bg-status-running animate-pulse"
  );

  const label =
    status === "online"
      ? "Connected"
      : status === "offline"
        ? "Not connected"
        : "Checking…";

  const content = (
    <button
      type="button"
      onClick={refresh}
      className={cn(
        "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs text-sidebar-foreground/70 transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground",
        collapsed && "size-8 justify-center px-0"
      )}
      aria-label={`${label}. ${baseUrl}. Click to recheck.`}
    >
      <span className={dotClass} aria-hidden />
      {/* The label, not the address. This sat in the sidebar footer of every
          screen reading "http://localhost:8080", which is the one piece of
          plumbing a person can do nothing with and never asked to see. The
          address is still one hover away, and on Settings in full, because an
          operator pointing the console at a different backend does need it. */}
      {!collapsed && <span className="min-w-0 flex-1 truncate">{label}</span>}
      {!collapsed && <RefreshCwIcon className="size-3 shrink-0 opacity-50" />}
    </button>
  );

  return (
    <Tooltip>
      <TooltipTrigger asChild>{content}</TooltipTrigger>
      <TooltipContent side="right" className="max-w-64">
        <div className="flex flex-col gap-1">
          <span className="font-medium">{label}</span>
          <span className="font-technical text-micro opacity-80">{baseUrl}</span>
          {status === "online" && config && (
            <span className="text-micro opacity-80">
              model {config.model} · {config.store} store
              {config.has_api_key ? "" : " · no API key set"}
            </span>
          )}
          {status === "offline" && error && (
            <span className="text-micro opacity-80">{error}</span>
          )}
        </div>
      </TooltipContent>
    </Tooltip>
  );
}

/** A compact pill for surfaces outside the sidebar (the chat header) that
 * still need the reachability signal without the full URL. */
export function HealthDot({ className }: { className?: string }) {
  const { status } = useBackendConfig();
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          className={cn(
            "inline-block size-2 rounded-full",
            status === "online" && "bg-status-completed",
            status === "offline" && "bg-status-failed",
            status === "checking" && "bg-status-running animate-pulse",
            className
          )}
        />
      </TooltipTrigger>
      <TooltipContent side="bottom">
        {status === "online" ? "Backend online" : status === "offline" ? "Backend unreachable" : "Checking…"}
      </TooltipContent>
    </Tooltip>
  );
}

export function BackendUnreachableNotice() {
  const { baseUrl, error, refresh } = useBackendConfig();
  return (
    <div className="flex items-center justify-between gap-3 border-b border-destructive/30 bg-destructive/10 px-4 py-2 text-sm text-destructive">
      <span>
        Can&rsquo;t reach the Psych backend
        {/* Empty when the console proxies to the backend on its own origin.
            Naming an address there would print a blank where a URL should be,
            and there is no second address to go and start anyway. */}
        {baseUrl === "" ? null : (
          <>
            {" "}
            at <span className="font-technical font-medium">{baseUrl}</span>
          </>
        )}
        {error ? `, ${error}` : ""}. Start it, or set{" "}
        <code className="font-technical">NEXT_PUBLIC_PSYCH_API</code>.
      </span>
      <Button size="sm" variant="outline" onClick={refresh} className="shrink-0 border-destructive/40">
        Retry
      </Button>
    </div>
  );
}
