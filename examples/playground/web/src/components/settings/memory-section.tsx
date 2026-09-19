"use client";

import { useCallback, useEffect, useState } from "react";
import { BrainIcon, Loader2Icon, Trash2Icon } from "lucide-react";
import { toast } from "sonner";

import { eraseMemories, forgetMemory, listMemories } from "@/lib/api";
import { describeApiError } from "@/lib/errors";
import { formatDayLabel } from "@/lib/format";
import type { MemoryOut } from "@/lib/types";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { FeatureRow, FeatureTable } from "@/components/ui/feature-table";
import { HelpTip } from "@/components/ui/help";
import { EmptyState } from "@/components/ui/page";
import { Skeleton } from "@/components/ui/skeleton";
import { SettingsSectionBlock } from "@/components/settings/section-nav";

/**
 * What your agents still know, and how to make them stop knowing it.
 *
 * A memory is a durable fact written by the `remember` tool during a
 * conversation and read back into the system prompt of every later one
 * (DESIGN.md §15). It is not conversation history: that is the log, and it is
 * already visible from a conversation's diagnostic views.
 *
 * ## Why deletion is the part worth building
 *
 * A list of accumulating facts is easy and proves nothing. §15 requires
 * `erase()` to remove everything for one end user, immediately and completely,
 * because a company using Psych will be asked by *their* customers to delete
 * their data, and answering that is the whole reason the operation exists. So
 * this section leads with the facts and ends with a button that finishes the
 * job, rather than offering a tidy-up that removes them one at a time and
 * half-finishes if somebody closes the tab.
 *
 * The confirmation is deliberate and the wording is deliberate: erasure is not
 * undoable, and a dialog that said "are you sure?" without saying what
 * disappears would be asking somebody to confirm something they have not been
 * told.
 */
export function MemorySection() {
  const [memories, setMemories] = useState<MemoryOut[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [erasing, setErasing] = useState(false);
  const [confirmErase, setConfirmErase] = useState(false);

  const load = useCallback(async () => {
    try {
      setMemories((await listMemories()).memories);
      setError(null);
    } catch (err) {
      setError(describeApiError(err));
    }
  }, []);

  useEffect(() => {
    // Deferred a tick: `load` sets state, and calling it inline would commit a
    // second update in the pass this mounts in.
    const id = setTimeout(() => void load(), 0);
    return () => clearTimeout(id);
  }, [load]);

  async function forgetOne(memory: MemoryOut) {
    try {
      await forgetMemory(memory.id);
      await load();
    } catch (err) {
      toast.error(describeApiError(err));
    }
  }

  async function eraseAll() {
    setErasing(true);
    try {
      await eraseMemories();
      await load();
      setConfirmErase(false);
      toast.success("Everything your agents remembered is gone.");
    } catch (err) {
      toast.error(describeApiError(err));
    } finally {
      setErasing(false);
    }
  }

  return (
    <SettingsSectionBlock
      id="memory"
      title="Memory"
      description={
        <span className="inline-flex flex-wrap items-center gap-1.5">
          {memories === null
            ? "Facts an agent chose to keep after a conversation ended."
            : memories.length === 1
              ? "1 fact your agents kept after a conversation ended."
              : `${memories.length} facts your agents kept after conversations ended.`}
          <HelpTip title="Memory" short="Facts that go into every later prompt.">
            <p>
              Every fact goes into the system prompt of each later conversation, in the order it
              was remembered. It is what makes an agent seem to know you.
            </p>
            <p>
              Nothing here is searched or ranked: Psych refuses to own retrieval, so a consumer
              who wants that supplies it themselves.
            </p>
          </HelpTip>
        </span>
      }
      actions={
        memories !== null && memories.length > 0 ? (
          <Button size="sm" variant="outline" onClick={() => setConfirmErase(true)}>
            <Trash2Icon /> Forget everything
          </Button>
        ) : undefined
      }
    >
      {error !== null && <p className="text-caption text-status-failed">{error}</p>}

      {memories === null && error === null && (
        <div className="flex flex-col gap-1.5">
          <Skeleton className="h-12 w-full rounded-lg" />
          <Skeleton className="h-12 w-full rounded-lg" />
        </div>
      )}

      {memories !== null && memories.length === 0 && (
        <EmptyState
          icon={BrainIcon}
          title="Nothing remembered yet"
          description="An agent remembers something when it decides a fact is worth keeping past the conversation. Nothing here is the ordinary case for a new account."
        />
      )}

      {memories !== null && memories.length > 0 && (
        <FeatureTable dense>
          {memories.map((memory) => (
            <FeatureRow
              key={memory.id}
              label={<span className="font-normal">{memory.content}</span>}
              detail={`Remembered ${formatDayLabel(memory.created_at)}`}
              control={
                <Button
                  size="icon-xs"
                  variant="ghost"
                  aria-label="Forget this"
                  onClick={() => void forgetOne(memory)}
                >
                  <Trash2Icon />
                </Button>
              }
            />
          ))}
        </FeatureTable>
      )}

      <Dialog open={confirmErase} onOpenChange={(open) => !open && setConfirmErase(false)}>
        <DialogContent className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>Forget everything?</DialogTitle>
            <DialogDescription>
              {memories?.length ?? 0} fact{memories?.length === 1 ? "" : "s"} will be deleted
              outright, not hidden. Your conversations stay available; only what the agents learned
              from them goes. This cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setConfirmErase(false)} disabled={erasing}>
              Keep them
            </Button>
            {/* The dialog is closed by `eraseAll` only once the request has
                succeeded, so a failed erase cannot close it and imply it
                worked. */}
            <Button variant="destructive" onClick={() => void eraseAll()} disabled={erasing}>
              {erasing ? <Loader2Icon className="animate-spin" /> : null}
              Forget everything
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </SettingsSectionBlock>
  );
}
