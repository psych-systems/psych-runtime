import Link from "next/link";
import { ActivityIcon, WaypointsIcon } from "lucide-react";

import { Button } from "@/components/ui/button";

export function RunViewSwitcher({
  runId,
  active,
  review = false,
}: {
  runId: string;
  active: "activity" | "trace";
  review?: boolean;
}) {
  const suffix = review ? "?review=1" : "";
  return (
    <nav aria-label="Run view" className="flex items-center rounded-lg border border-border bg-muted/30 p-0.5">
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
    </nav>
  );
}
