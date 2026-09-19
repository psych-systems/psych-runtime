"use client";

import { useState } from "react";
import { CheckIcon, ChevronsUpDownIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { cn } from "@/lib/utils";
import type { ModelQuote } from "@/lib/types";

/**
 * "$1.25 in · $10 out" per million tokens. Shown beside a model id wherever
 * one is chosen, so the price is part of the choice rather than a surprise on
 * the first Run's cost line. Nothing is shown for a model with no known
 * rate: an unknown price is unknown, never zero.
 */
export function formatQuote(quote: ModelQuote | undefined): string | null {
  if (quote === undefined || quote.input == null || quote.output == null) return null;
  const currency = quote.currency ?? "USD";
  const symbol = currency === "USD" ? "$" : `${currency} `;
  return `${symbol}${trimRate(quote.input)} in · ${symbol}${trimRate(quote.output)} out /1M`;
}

function trimRate(value: number | string): string {
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n)) return String(value);
  // Whole dollars stay whole ($10), cents keep two places ($2.50, $0.20),
  // and a rate that needs a third gets it ($0.075), never more.
  if (n >= 1) return n.toFixed(2).replace(/\.00$/, "");
  return n.toFixed(3).replace(/0$/, "");
}

/**
 * Picks the model an agent runs on: the names its provider actually serves,
 * without refusing a name it has never heard of.
 *
 * ## Why not a `<datalist>`, which this replaces
 *
 * A datalist satisfied the requirement on paper -- suggestions when there are
 * suggestions, free text either way -- and failed it in practice. It has no
 * click-to-open in most browsers, so the only affordance is a chevron that
 * does nothing when pressed; it filters by prefix, so typing `gpt` hides
 * `azure/gpt-5.6-luna`, which is exactly the id somebody is reaching for; and
 * whether the popup appears at all varies by browser and build. A control that
 * silently does nothing reads as broken software rather than as a text field.
 *
 * ## Why not a `<select>` either
 *
 * `known_models()` returns nothing when a provider will not say what it serves
 * (DESIGN.md §19), which is ordinary for a proxy rather than a
 * misconfiguration. A closed dropdown would be a form nobody could complete on
 * those providers. That constraint is what a plain select gets wrong and is
 * why the datalist was chosen in the first place; it is preserved here.
 *
 * So: a combobox that opens on click, filters on substring, and always offers
 * whatever was typed as a selectable option. Typing a model this playground
 * has never seen is a first-class action, not a fallback -- a provider can
 * serve a model its `/models` endpoint omits, and the person publishing the
 * agent is the one who knows.
 */
export function ModelField({
  id,
  value,
  onChange,
  models,
  prices,
  detail,
  placeholder,
  invalid,
}: {
  /** Put on the trigger, so a `LabelWithHelp` beside it points somewhere. */
  id?: string;
  value: string;
  onChange: (next: string) => void;
  /** What the provider said it serves. Empty is normal, not an error. */
  models: string[];
  /** List prices by model id, shown beside each one that has a rate. */
  prices?: Record<string, ModelQuote>;
  /** The backend's own sentence about where this list came from, or why it is
   *  empty. Rendered verbatim rather than reworded, so the reason a list is
   *  short reaches the person who has to act on it. */
  detail: string | null;
  placeholder: string;
  invalid?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");

  const typed = query.trim();
  // Offered whenever it is not already one of the known names, so a model the
  // provider did not list is still one click away rather than needing the
  // field to be closed and edited by hand.
  const showCustom = typed !== "" && !models.includes(typed);

  function choose(next: string) {
    onChange(next);
    setQuery("");
    setOpen(false);
  }

  return (
    <>
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <Button
            id={id}
            type="button"
            variant="outline"
            role="combobox"
            aria-expanded={open}
            aria-invalid={invalid}
            className="h-8 w-full justify-between px-2.5 font-technical font-normal"
          >
            <span className={cn("truncate", value === "" && "text-muted-foreground")}>
              {value === "" ? placeholder : value}
            </span>
            <span className="flex shrink-0 items-center gap-2">
              {value !== "" && formatQuote(prices?.[value]) && (
                <span className="hidden text-micro text-muted-foreground sm:inline">
                  {formatQuote(prices?.[value])}
                </span>
              )}
              <ChevronsUpDownIcon className="size-3.5 shrink-0 opacity-50" />
            </span>
          </Button>
        </PopoverTrigger>
        <PopoverContent className="w-(--radix-popover-trigger-width) p-0" align="start">
          <Command
            // The typed value is offered as its own item, so cmdk must not
            // filter it out for failing to match itself.
            filter={(itemValue, search) =>
              itemValue.toLowerCase().includes(search.toLowerCase()) ? 1 : 0
            }
          >
            <CommandInput
              placeholder="Search or type a model id"
              value={query}
              onValueChange={setQuery}
            />
            <CommandList>
              <CommandEmpty>Type a model id to use it.</CommandEmpty>
              {showCustom && (
                <CommandGroup heading="Use what you typed">
                  <CommandItem value={typed} onSelect={() => choose(typed)}>
                    <CheckIcon
                      className={cn("size-3.5", value === typed ? "opacity-100" : "opacity-0")}
                    />
                    <span className="font-technical">{typed}</span>
                  </CommandItem>
                </CommandGroup>
              )}
              {models.length > 0 && (
                <CommandGroup heading="Offered by this provider">
                  {models.map((name) => {
                    const quote = formatQuote(prices?.[name]);
                    const note = prices?.[name]?.note;
                    return (
                      <CommandItem key={name} value={name} onSelect={() => choose(name)}>
                        <CheckIcon
                          className={cn("size-3.5", value === name ? "opacity-100" : "opacity-0")}
                        />
                        <span className="flex min-w-0 flex-1 flex-col">
                          <span className="truncate font-technical">{name}</span>
                          {note && <span className="text-micro text-muted-foreground">{note}</span>}
                        </span>
                        {quote && (
                          <span className="ml-2 shrink-0 text-micro text-muted-foreground">
                            {quote}
                          </span>
                        )}
                      </CommandItem>
                    );
                  })}
                </CommandGroup>
              )}
            </CommandList>
          </Command>
        </PopoverContent>
      </Popover>
      {detail !== null && <p className="text-caption text-muted-foreground">{detail}</p>}
    </>
  );
}
