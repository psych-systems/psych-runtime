import Link from "next/link";
import { ActivityIcon, WaypointsIcon, WorkflowIcon } from "lucide-react";

import { Button } from "@/components/ui/button";

/**
 * The three ways of looking at one run.
 *
 * Workflow is offered only for a run that has one. An entry that leads to
 * "this run is not a workflow" on two thirds of the runs in the console would
 * be a worse tab than no tab, so the caller answers the question before the
 * switcher is drawn.
 */
export function RunViewSwitcher({
  runId,
  active,
  review = false,
  isWorkflow = false,
}: {
  runId: string;
  active: "activity" | "trace" | "workflow";
  review?: boolean;
  isWorkflow?: boolean;
}) {
  const suffix = review ? "?review=1" : "";
  return (
    <nav aria-label="Run view" className="flex flex-wrap items-center rounded-lg border border-border bg-muted/30 p-0.5">
      <Button asChild variant={active === "activity" ? "secondary" : "ghost"} size="sm">
        <Link href={`/activity/${runId}${suffix}`} aria-label="Activity details" aria-current={active === "activity" ? "page" : undefined}>
          <ActivityIcon className="size-3.5" />
          Activity
        </Link>
      </Button>
      <Button asChild variant={active === "trace" ? "secondary" : "ghost"} size="sm">
        <Link href={`/activity/${runId}/trace${suffix}`} aria-label="Execution trace" aria-current={active === "trace" ? "page" : undefined}>
          <WaypointsIcon className="size-3.5" />
          Trace
        </Link>
      </Button>
      {isWorkflow && (
        <Button asChild variant={active === "workflow" ? "secondary" : "ghost"} size="sm">
          <Link href={`/activity/${runId}/workflow${suffix}`} aria-label="Workflow view" aria-current={active === "workflow" ? "page" : undefined}>
            <WorkflowIcon className="size-3.5" />
            Workflow
          </Link>
        </Button>
      )}
    </nav>
  );
}
