"use client";

import { SlidersHorizontalIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { DetailRow, TechnicalDetails } from "@/components/ui/page";
import {
  Popover,
  PopoverContent,
  PopoverDescription,
  PopoverHeader,
  PopoverTitle,
  PopoverTrigger,
} from "@/components/ui/popover";

interface ConversationSettingsProps {
  /** Who this conversation runs as: the signed-in account. */
  accountName: string;
  runId: string | null;
  versionHash: string | null;
}

/**
 * The quiet control in the header.
 *
 * Everything technical about a conversation lives here and nowhere else, and
 * none of it belongs beside the message box, which is for writing messages.
 *
 * ## It used to be two text boxes, and that was the bug
 *
 * This popover let a person type a workspace and an "acting as" name, and the
 * chat sent both to `POST /api/runs`, which built its `Scope` from them. So the
 * isolation boundary was whatever the browser said it was: type someone else's
 * workspace and read their conversations. What looked like a preference was the
 * only thing standing between two people's data.
 *
 * Both are now decided by the session and neither is editable, so they are
 * reported rather than offered. That is the honest shape for a fact the person
 * cannot change from here, and one fewer control that looks like it does
 * something.
 */
export function ConversationSettings({
  accountName,
  runId,
  versionHash,
}: ConversationSettingsProps) {
  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button type="button" variant="ghost" size="icon-sm" aria-label="Conversation details">
          <SlidersHorizontalIcon />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-80">
        <PopoverHeader>
          <PopoverTitle>Conversation details</PopoverTitle>
          <PopoverDescription>
            This conversation, and everything it can reach, belongs to your account.
          </PopoverDescription>
        </PopoverHeader>

        <div className="flex flex-col gap-3 pt-1">
          <DetailRow label="Running as">{accountName}</DetailRow>

          {(runId !== null || versionHash !== null) && (
            <TechnicalDetails className="pt-1">
              {runId !== null && (
                <DetailRow label="Run">
                  <span className="font-technical">{runId}</span>
                </DetailRow>
              )}
              {versionHash !== null && (
                <DetailRow label="Agent version">
                  <span className="font-technical">{versionHash}</span>
                </DetailRow>
              )}
            </TechnicalDetails>
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}
