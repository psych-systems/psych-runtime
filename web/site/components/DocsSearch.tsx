"use client";

import { useMemo } from "react";
import { useDocsSearch } from "fumadocs-core/search/client";
import { staticClient } from "fumadocs-core/search/client/orama-static";
import type { ReactSortedResult } from "fumadocs-core/search";
import {
  SearchDialog,
  SearchDialogClose,
  SearchDialogContent,
  SearchDialogFooter,
  SearchDialogHeader,
  SearchDialogIcon,
  SearchDialogInput,
  SearchDialogList,
  SearchDialogListItem,
  SearchDialogOverlay,
} from "fumadocs-ui/components/dialog/search";
import type { SharedProps } from "fumadocs-ui/contexts/search";

/**
 * Search results grouped by the page they are in.
 *
 * The index holds one entry per heading and per paragraph, and the stock dialog
 * renders that list flat. Searching "approval" therefore returned the approvals
 * guide many times over: the page, then every heading in it, then whole
 * paragraphs of body text, each row repeating the same breadcrumb. A reader had
 * to read the results to discover they were all one page.
 *
 * This keeps the page rows and folds the matches under them, at most three per
 * page and each trimmed to a line. What is lost is deep-linking to the fourth
 * match on a page; what is gained is seeing, in one screen, which pages are
 * worth opening at all.
 */
const MATCHES_PER_PAGE = 3;
const EXCERPT = 78;

type Item = ReactSortedResult & { subs?: ReactSortedResult[] };

function groupByPage(results: ReactSortedResult[]): Item[] {
  const out: Item[] = [];
  for (const r of results) {
    // A page row opens a group. Anything after it pointing into the same page
    // belongs to that group; the index emits them in that order.
    const parent =
      r.type === "page"
        ? undefined
        : out.find((p) => r.url === p.url || r.url.startsWith(`${p.url}#`));
    if (!parent) {
      out.push({ ...r, subs: [] });
      continue;
    }
    parent.subs ??= [];
    if (parent.subs.length < MATCHES_PER_PAGE) parent.subs.push(r);
  }
  return out;
}

/**
 * "Docs > next > Guides > Approvals" on every row, where the first two crumbs
 * are the same on every result there will ever be. Drop them and the crumb
 * says the one thing that varies.
 */
function shorten<T>(item: T): T {
  const crumbs = (item as { breadcrumbs?: unknown }).breadcrumbs;
  return Array.isArray(crumbs) && crumbs.length > 2
    ? { ...item, breadcrumbs: crumbs.slice(2) }
    : item;
}

function trim(content: ReactSortedResult["content"]) {
  if (typeof content !== "string") return content;
  const flat = content.replace(/\s+/g, " ").trim();
  if (flat.length <= EXCERPT) return flat;
  // Cut at a word boundary. Slicing mid-word and appending an ellipsis reads
  // like a rendering bug rather than a deliberate excerpt.
  const cut = flat.slice(0, EXCERPT);
  const space = cut.lastIndexOf(" ");
  return `${(space > EXCERPT * 0.6 ? cut.slice(0, space) : cut).trimEnd()}…`;
}

export function DocsSearch(props: SharedProps) {
  const client = staticClient({ from: "/api/search" });
  const { search, setSearch, query } = useDocsSearch({ client });
  const items = useMemo(
    () => (query.data && query.data !== "empty" ? groupByPage(query.data) : null),
    [query.data],
  );

  return (
    <SearchDialog
      search={search}
      onSearchChange={setSearch}
      isLoading={query.isLoading}
      {...props}
    >
      <SearchDialogOverlay />
      <SearchDialogContent>
        <SearchDialogHeader>
          <SearchDialogIcon />
          <SearchDialogInput />
          <SearchDialogClose />
        </SearchDialogHeader>
        <SearchDialogList
          items={items}
          Item={({ item, onClick }) => {
            const grouped = item as Item;
            return (
              <div className="psych-result">
                <SearchDialogListItem item={shorten(item)} onClick={onClick} />
                {grouped.subs?.length ? (
                  <ul className="psych-result-subs">
                    {grouped.subs.map((sub) => (
                      <li key={sub.id}>
                        <SearchDialogListItem
                          item={{ ...shorten(sub), content: trim(sub.content) }}
                          onClick={onClick}
                        />
                      </li>
                    ))}
                  </ul>
                ) : null}
              </div>
            );
          }}
        />
      </SearchDialogContent>
      <SearchDialogFooter />
    </SearchDialog>
  );
}

export default DocsSearch;
