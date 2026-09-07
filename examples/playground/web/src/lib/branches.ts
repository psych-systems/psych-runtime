/**
 * Folds `GET /api/runs` into the conversations a person had, and answers the
 * one question a branched conversation asks: which of these messages is on
 * screen, and what are the others?
 *
 * A conversation is a chain of Runs: a second message continues the first Run
 * rather than extending it, so `continues_run_id` is the only link between two
 * turns of the same exchange. Branching makes that chain a *tree*: two Runs
 * may continue the same predecessor, because somebody asked the same question
 * a second way and wanted to keep both answers.
 *
 * A tree is still one conversation. That is the correction this file carries,
 * and it was worth getting wrong once to see it. Every branch used to be its
 * own row here, so re-wording a question appeared to start a second chat, and
 * deleting either one had to reason about what the other still needed. Nobody
 * else works that way: an edited message makes a
 * version you page between with small arrows, inside the chat you are already
 * in. So the fold is over `conversation_id`, one row per conversation, and the
 * branches inside it are navigation rather than rows.
 *
 * Forking is the other operation and it does make a row: a fork diverges at
 * the same message a branch would, but takes a conversation id of its own, so
 * it is a chat in its own right that happens to replay somebody else's Runs as
 * its history.
 *
 * Both ids come from the backend, stamped at dispatch, and neither is derived
 * here for the reason the backend does not derive them either: the branch
 * point has two children and nothing in the chain says which future is which.
 * A Run dispatched before conversations had ids carries none, and is grouped
 * by the root of its chain instead -- exactly the old behaviour, and correct,
 * because such a Run cannot have branched.
 *
 * Distinct from `lib/conversation.ts`, which folds one conversation's *records*
 * into the items a transcript renders. This one folds *Runs* into the rows a
 * list renders.
 *
 * Pure: same list in, same rows out. No fetching, no clock.
 */

import type { RunSummary } from "@/lib/types";

/** One conversation: the whole tree, every branch of it. */
export interface Conversation {
  /** The backend's conversation id, or the empty string for a Run dispatched
   *  before conversations had one. Never a list key on its own -- see `key`. */
  conversationId: string;
  /** Stable key for a list. The conversation id when there is one, and the
   *  root Run's id otherwise. */
  key: string;
  /** The first Run of this conversation. Its message is the row's title. For a
   *  fork that is the message the fork diverged at, which is what tells it
   *  apart from the conversation it came from. */
  head: RunSummary;
  /** The newest Run in this conversation, on any branch. Its state is the
   *  row's state, its id is what a row links to, and the branch it sits on is
   *  the one that opens. */
  latest: RunSummary;
  /** Everything a reader arriving at `latest` sees, oldest first. For a fork
   *  this reaches back into the conversation it was taken from, because that
   *  history really is replayed. */
  runs: RunSummary[];
  /** Only this conversation's own Runs, every branch, oldest first. What "is
   *  this row the one open on screen" is answered against: a fork replays
   *  another conversation's Runs, and matching on `runs` would light up both
   *  rows at once. */
  ownRuns: RunSummary[];
  /** This conversation was forked out of another one, so its history starts
   *  partway through a chat somebody else's row also tells. */
  forked: boolean;
  /** How many branches this conversation has. One unless a message in it has
   *  been asked again. */
  branchCount: number;
  /** `head.started_at`: when this conversation began. */
  startedAt: string;
  /** `latest.settled_at`, or null while the newest turn is still going. */
  settledAt: string | null;
  /** Wall clock from the first message to the last settlement, or null while
   *  it has not finished. Not a sum of the Runs: the gaps between turns are a
   *  person reading and typing, and hiding them would make a two minute
   *  exchange look like four seconds of work. */
  elapsedSeconds: number | null;
}

function epoch(iso: string): number {
  const ms = new Date(iso).getTime();
  return Number.isNaN(ms) ? 0 : ms;
}

function byTime(a: RunSummary, b: RunSummary): number {
  return epoch(a.started_at) - epoch(b.started_at);
}

/**
 * Every Run from `run` back to the oldest one in this list, oldest first,
 * `run` included.
 *
 * A parent outside the list -- purged by the consumer's own retention, or
 * simply not in this response -- ends the walk and makes that Run the oldest
 * this list can honestly show. The cycle guard is for a link that cannot loop
 * and would hang a sidebar rather than render a wrong row if it ever did.
 */
function ancestry(
  run: RunSummary,
  byId: ReadonlyMap<string, RunSummary>,
): RunSummary[] {
  const chain = [run];
  const seen = new Set<string>([run.run_id]);
  for (;;) {
    const parentId = chain[0].continues_run_id;
    if (!parentId || seen.has(parentId)) return chain;
    const parent = byId.get(parentId);
    if (!parent) return chain;
    seen.add(parentId);
    chain.unshift(parent);
  }
}

/** The conversation a Run belongs to, falling back to the root of its own
 *  chain for a Run recorded before conversations had ids. That is what a
 *  conversation was before forking existed, so those Runs group exactly as
 *  they always did. */
function conversationKey(
  run: RunSummary,
  byId: ReadonlyMap<string, RunSummary>,
): string {
  if (run.conversation_id !== "") return `c:${run.conversation_id}`;
  return `r:${ancestry(run, byId)[0].run_id}`;
}

/** Newest first, each one's Runs oldest first. One row per conversation. */
export function groupConversations(
  runs: readonly RunSummary[],
): Conversation[] {
  const byId = new Map<string, RunSummary>();
  for (const run of runs) byId.set(run.run_id, run);

  const groups = new Map<string, RunSummary[]>();
  for (const run of runs) {
    const key = conversationKey(run, byId);
    const group = groups.get(key);
    if (group) group.push(run);
    else groups.set(key, [run]);
  }

  const conversations: Conversation[] = [];
  for (const [key, group] of groups) {
    const ownRuns = [...group].sort(byTime);
    const head = ownRuns[0];
    const latest = ownRuns[ownRuns.length - 1];
    const settledAt = latest.settled_at;
    conversations.push({
      conversationId: head.conversation_id,
      key,
      head,
      latest,
      runs: ancestry(latest, byId),
      ownRuns,
      forked: ancestry(head, byId).length > 1,
      branchCount: new Set(ownRuns.map((run) => run.branch_id)).size,
      startedAt: head.started_at,
      settledAt,
      elapsedSeconds:
        settledAt === null
          ? null
          : (epoch(settledAt) - epoch(head.started_at)) / 1000,
    });
  }

  conversations.sort(
    (a, b) => epoch(b.latest.started_at) - epoch(a.latest.started_at),
  );
  return conversations;
}

/** The versions of one message: the Run on screen and its siblings, oldest
 *  first, with the position of the one asked about. */
export interface BranchChoice {
  versions: RunSummary[];
  index: number;
}

/**
 * The other ways this message was asked, or null when it was only asked once.
 *
 * Siblings are the Runs continuing the same predecessor, which is exactly what
 * branching produces: the new Run continues the branched message's parent, so
 * the two sit side by side under it. A conversation's opening message has no
 * predecessor, and its branches are the other Runs of the conversation with no
 * predecessor either.
 *
 * Restricted to one conversation, and that restriction is load-bearing: a fork
 * also continues the same predecessor, and counting it here would offer to
 * page from a chat into a different one.
 */
export function branchChoice(
  runs: readonly RunSummary[],
  runId: string,
): BranchChoice | null {
  const byId = new Map<string, RunSummary>();
  for (const run of runs) byId.set(run.run_id, run);
  const run = byId.get(runId);
  if (!run) return null;

  const conversation = conversationKey(run, byId);
  const parent = run.continues_run_id ?? "";
  const versions = runs
    .filter(
      (other) =>
        (other.continues_run_id ?? "") === parent &&
        conversationKey(other, byId) === conversation,
    )
    .sort(byTime);
  if (versions.length < 2) return null;
  return {
    versions,
    index: versions.findIndex((other) => other.run_id === runId),
  };
}

/**
 * The last Run on this Run's own branch: what opening that version of a
 * message should land on.
 *
 * Only children sharing the branch are followed, which is what branch ids are
 * for. Following "a child" would take an arbitrary one of two futures at every
 * point the conversation was branched, so paging to a version would sometimes
 * land in a different version's tail. `max` decides between two children that
 * should not both exist -- two callers racing a continuation at one parent --
 * arbitrarily but deterministically, so the same message opens every time.
 */
export function newestOnBranch(
  runs: readonly RunSummary[],
  runId: string,
): string {
  const children = new Map<string, RunSummary[]>();
  const byId = new Map<string, RunSummary>();
  for (const run of runs) {
    byId.set(run.run_id, run);
    const parentId = run.continues_run_id;
    if (!parentId) continue;
    const group = children.get(parentId);
    if (group) group.push(run);
    else children.set(parentId, [run]);
  }

  const start = byId.get(runId);
  if (!start) return runId;
  const seen = new Set<string>([runId]);
  let current = start;
  for (;;) {
    const same = (children.get(current.run_id) ?? []).filter(
      (child) => child.branch_id === current.branch_id,
    );
    if (same.length === 0) return current.run_id;
    const next = same.reduce((a, b) => (a.run_id > b.run_id ? a : b));
    if (seen.has(next.run_id)) return current.run_id;
    seen.add(next.run_id);
    current = next;
  }
}
