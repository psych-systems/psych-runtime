/**
 * One chosen connection, and how much of it this agent may use.
 *
 * `allow` empty means everything the connection offers, which is what
 * `McpServer.allow` means on the wire too. The picker that edits this now
 * lives in `capabilities-table.tsx` as a row per connection; what is left
 * here is the shape both it and the publish agree on.
 */
export interface ConnectionChoice {
  name: string;
  allow: string[];
}
