"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Loader2Icon, RotateCcwIcon } from "lucide-react";

import { replayRun } from "@/lib/api";
import { describeApiError } from "@/lib/errors";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { FieldError } from "@/components/settings/validation";
import { BreakpointPicker } from "@/components/workflows/breakpoint-picker";

/**
 * Run this workflow again from one step.
 *
 * A new run rather than a change to this one, which is what makes it safe to
 * do repeatedly: the run that went wrong stays readable beside the one that
 * fixed it. The input starts as the input this run had, because the usual
 * reason to replay is that one field of it was wrong.
 */
export function ReplayDialog({
  runId,
  fromStep,
  input,
  stepNames,
  open,
  onOpenChange,
}: {
  runId: string;
  fromStep: string;
  input: Record<string, unknown>;
  stepNames: string[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const router = useRouter();
  const [text, setText] = useState(() => JSON.stringify(input, null, 2));
  const [breakpoints, setBreakpoints] = useState<string[]>([]);
  const [stepMode, setStepMode] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function go() {
    let parsed: Record<string, unknown> | null;
    try {
      const value: unknown = text.trim() === "" ? null : JSON.parse(text);
      if (value !== null && (typeof value !== "object" || Array.isArray(value))) {
        setError("The input is an object of fields.");
        return;
      }
      parsed = value as Record<string, unknown> | null;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Not valid JSON.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const { run_id } = await replayRun(runId, {
        from_step: fromStep,
        input: parsed,
        breakpoints: breakpoints.length > 0 ? breakpoints : undefined,
        step_mode: stepMode || undefined,
      });
      onOpenChange(false);
      router.push(`/activity/${run_id}/workflow`);
    } catch (err) {
      setError(describeApiError(err));
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Replay from {fromStep}</DialogTitle>
          <DialogDescription>
            Everything before this step is carried over. This step runs again, and so does
            everything after it, as a new run beside this one.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-4">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="replay-input">Input, as JSON</Label>
            <Textarea
              id="replay-input"
              value={text}
              rows={8}
              spellCheck={false}
              className="font-technical text-caption"
              onChange={(event) => {
                setText(event.target.value);
                setError(null);
              }}
            />
          </div>

          <div className="flex flex-col gap-2">
            <Label>Stop before these steps</Label>
            <BreakpointPicker
              stepNames={stepNames}
              value={breakpoints}
              onChange={setBreakpoints}
            />
            <label className="flex w-fit items-center gap-2 text-caption text-muted-foreground">
              <input
                type="checkbox"
                className="size-3.5 accent-primary"
                checked={stepMode}
                onChange={(event) => setStepMode(event.target.checked)}
              />
              Stop before every step
            </label>
          </div>

          <FieldError message={error ?? undefined} />
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={() => void go()} disabled={busy}>
            {busy ? <Loader2Icon className="animate-spin" /> : <RotateCcwIcon />} Replay
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
