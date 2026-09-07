import { createFromSource } from "fumadocs-core/search/server";
import { source } from "@/lib/source";

/**
 * The search index, built once at build time from the same tree the pages
 * render, so a result can never point at a page that no longer exists.
 *
 * `staticGET` rather than `GET`: this site exports to plain HTML, so there is no
 * server to answer a query. The whole index ships as one file and the browser
 * searches it. That is affordable because the docs are tens of pages, not
 * thousands, and it removes the search outage that a server-side index invents.
 */
export const revalidate = false;
export const { staticGET: GET } = createFromSource(source);
