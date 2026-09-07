"use client";

import { useMemo, useState } from "react";
import { SearchIcon, XIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

export interface CheckListOption {
  value: string;
  label: string;
  hint?: string;
  /** Render the label in the technical face. True for a tool name, which is
   *  an identifier the model actually calls; false for anything a person
   *  typed themselves. */
  mono?: boolean;
}

interface CheckListProps {
  options: CheckListOption[];
  selected: readonly string[];
  onChange: (next: string[]) => void;
  searchPlaceholder: string;
  emptyText: string;
  /** Shown above the list, e.g. "351 tools". */
  countLabel?: (total: number) => string;
  disabled?: boolean;
}

/**
 * How many rows are put in the DOM at once.
 *
 * One connection in this playground offers 351 tools, and the picker this
 * replaces was a popover that rendered every option unconditionally: opening
 * it built 351 rows, and the only way to reach one was to scroll a 300px box
 * past the other 350. A cap plus a search box is what makes that list
 * usable, and the count line below says plainly how many matches are not on
 * screen rather than pretending the list ended.
 */
const RENDER_CAP = 60;

/**
 * A searchable list of checkboxes for picking many of a named set.
 *
 * Always visible rather than hidden behind a popover: an allow-list someone
 * is building is the thing they are looking at, and a popover that closes on
 * every click made picking twelve tools twelve round trips.
 */
export function CheckList({
  options,
  selected,
  onChange,
  searchPlaceholder,
  emptyText,
  countLabel,
  disabled,
}: CheckListProps) {
  const [query, setQuery] = useState("");
  const selectedSet = useMemo(() => new Set(selected), [selected]);

  const matches = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (needle === "") return options;
    return options.filter(
      (option) =>
        option.label.toLowerCase().includes(needle) ||
        (option.hint?.toLowerCase().includes(needle) ?? false)
    );
  }, [options, query]);

  const visible = matches.slice(0, RENDER_CAP);
  const hidden = matches.length - visible.length;

  function toggle(value: string) {
    if (disabled) return;
    onChange(
      selectedSet.has(value) ? selected.filter((v) => v !== value) : [...selected, value]
    );
  }

  function selectAllMatching() {
    const next = new Set(selected);
    for (const option of matches) next.add(option.value);
    onChange([...next]);
  }

  return (
    <div className={cn("flex flex-col gap-2", disabled && "pointer-events-none opacity-60")}>
      <div className="relative">
        <SearchIcon
          className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground"
          aria-hidden
        />
        <Input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={searchPlaceholder}
          className="pl-8"
        />
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2 text-caption text-muted-foreground">
        <span>
          {countLabel ? countLabel(options.length) : `${options.length} available`}
          {query.trim() !== "" && `, ${matches.length} matching`}
          {selected.length > 0 && `, ${selected.length} chosen`}
        </span>
        <span className="flex items-center gap-1">
          {matches.length > 0 && matches.some((o) => !selectedSet.has(o.value)) && (
            <Button type="button" size="xs" variant="ghost" onClick={selectAllMatching}>
              Add {query.trim() === "" ? "all" : "all matching"}
            </Button>
          )}
          {selected.length > 0 && (
            <Button type="button" size="xs" variant="ghost" onClick={() => onChange([])}>
              <XIcon /> Clear
            </Button>
          )}
        </span>
      </div>

      <div className="max-h-72 overflow-y-auto rounded-lg border border-border">
        {visible.length === 0 ? (
          <p className="px-3 py-6 text-center text-caption text-muted-foreground">{emptyText}</p>
        ) : (
          <ul className="divide-y divide-border/60">
            {visible.map((option) => {
              const checked = selectedSet.has(option.value);
              return (
                <li key={option.value}>
                  <label className="flex cursor-pointer items-start gap-2.5 px-3 py-2 transition-colors hover:bg-surface/60">
                    <Checkbox
                      checked={checked}
                      onCheckedChange={() => toggle(option.value)}
                      className="mt-0.5"
                    />
                    <span className="flex min-w-0 flex-col gap-0.5">
                      <span
                        className={cn(
                          "truncate text-body leading-tight",
                          option.mono && "font-technical"
                        )}
                      >
                        {option.label}
                      </span>
                      {option.hint && (
                        <span className="line-clamp-2 text-caption text-muted-foreground">
                          {option.hint}
                        </span>
                      )}
                    </span>
                  </label>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      {hidden > 0 && (
        <p className="text-caption text-muted-foreground">
          {hidden} more match. Narrow the search to see them.
        </p>
      )}

      {selected.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {selected.map((value) => {
            const option = options.find((o) => o.value === value);
            return (
              <span
                key={value}
                className="inline-flex max-w-64 items-center gap-1 rounded-full bg-surface px-2.5 py-1 text-caption text-surface-foreground"
              >
                <span className={cn("truncate", option?.mono !== false && "font-technical")}>
                  {option?.label ?? value}
                </span>
                <button
                  type="button"
                  onClick={() => toggle(value)}
                  aria-label={`Remove ${option?.label ?? value}`}
                  className="shrink-0 rounded-full text-muted-foreground transition-colors hover:text-destructive"
                >
                  <XIcon className="size-3" aria-hidden />
                </button>
              </span>
            );
          })}
        </div>
      )}
    </div>
  );
}
