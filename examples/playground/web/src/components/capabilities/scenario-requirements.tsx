import { BoxIcon, CpuIcon, DatabaseIcon } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import type { ScenarioRequirement } from "@/lib/types";

const REQUIREMENT_LABEL: Record<ScenarioRequirement, string> = {
  postgres: "Postgres",
  mysql: "MySQL",
  dynamodb: "DynamoDB",
  "model-provider": "model provider",
  "container-runtime": "container runtime",
};

const REQUIREMENT_ICON: Record<ScenarioRequirement, typeof DatabaseIcon> = {
  postgres: DatabaseIcon,
  mysql: DatabaseIcon,
  dynamodb: DatabaseIcon,
  "model-provider": CpuIcon,
  "container-runtime": BoxIcon,
};

/** What has to be real and reachable for one scenario to run at all --
 * `ScenarioInfo.requires`, badged. Empty means the scenario needs nothing
 * beyond the interpreter itself. */
export function ScenarioRequirements({ requires }: { requires: ScenarioRequirement[] }) {
  if (requires.length === 0) return null;
  return (
    <>
      {requires.map((req) => {
        const Icon = REQUIREMENT_ICON[req];
        return (
          <Badge key={req} variant="outline" className="text-micro text-muted-foreground">
            <Icon /> {REQUIREMENT_LABEL[req]}
          </Badge>
        );
      })}
    </>
  );
}
