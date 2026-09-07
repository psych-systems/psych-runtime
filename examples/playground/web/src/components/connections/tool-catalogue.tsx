"use client";

import { useMemo, useState } from "react";
import { SearchIcon } from "lucide-react";

import { Input } from "@/components/ui/input";

/**
 * The tool names a connected server offers, searchable.
 *
 * One real server in this playground answers with 351 tools. The old page
 * printed all of them as one comma-joined line, which is unreadable and
 * unusable for the one job the list has: an allow-list is written against
 * these names, so a person has to be able to find one. Nothing is rendered
 * until it matches, and the count says what is being hidden.
 */
const PAGE = 60;

export function ToolCatalogue({ tools }: { tools: string[] }) {
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState(false);

  const matches = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (needle === "") return tools;
    return tools.filter((tool) => tool.toLowerCase().includes(needle));
  }, [tools, query]);

  const shown = expanded ? matches : matches.slice(0, PAGE);

  if (tools.length === 0) {
    return (
      <p className="text-caption text-muted-foreground">
        This connection reported no tools.
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-2.5">
      <div className="relative">
        <SearchIcon className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
        <Input
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
            setExpanded(false);
          }}
          placeholder={`Search ${tools.length} tools`}
          className="h-8 pl-8 font-technical text-caption"
          aria-label="Search this connection's tools"
        />
      </div>

      {matches.length === 0 ? (
        <p className="text-caption text-muted-foreground">
          Nothing here matches &ldquo;{query.trim()}&rdquo;.
        </p>
      ) : (
        <>
          <div className="flex flex-wrap gap-1">
            {shown.map((tool) => (
              <span
                key={tool}
                className="rounded-md bg-surface px-1.5 py-0.5 font-technical text-micro text-surface-foreground"
              >
                {tool}
              </span>
            ))}
          </div>
          {matches.length > shown.length && (
            <button
              type="button"
              onClick={() => setExpanded(true)}
              className="w-fit text-caption font-medium text-primary underline underline-offset-2"
            >
              Show the other {matches.length - shown.length}
            </button>
          )}
        </>
      )}
    </div>
  );
}
