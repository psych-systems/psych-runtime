"use client";

import { useMemo } from "react";
import { AlertTriangleIcon, FlaskConicalIcon, RefreshCwIcon, SquareStackIcon } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { EmptyState, Page, PageHeader, Section, Stat } from "@/components/ui/page";
import { Skeleton } from "@/components/ui/skeleton";
import { BackendUnreachableNotice } from "@/components/app-shell/health-indicator";
import { useBackendConfig } from "@/components/app-shell/backend-config-provider";
import { ScenarioCard } from "@/components/capabilities/scenario-card";
import { useScenarioRunner } from "@/components/capabilities/use-scenario-runner";
import { useScenarios } from "@/components/capabilities/use-scenarios";
import type { ScenarioSummary } from "@/lib/types";

/**
 * The one screen where the runtime's own vocabulary belongs.
 *
 * Everywhere else in this console, a design-document section number or a
 * store's internal state is something to keep out of a person's way. Here the
 * reader is evaluating whether the runtime does what it claims, so the claim
 * and its citation are the content. That is why Capabilities sits in its own
 * nav group rather than beside Chat.
 */

/**
 * `GET /api/scenarios` returns modules in `app.scenarios.SCENARIOS`' own
 * declared order: DESIGN.md §23's ten definition-of-done items first, in
 * order, then the further capabilities the suite also demonstrates. Grouping
 * by position rather than by `design_ref` matters -- `tenant-isolation` cites
 * §23.6 (the MCP item's tenant-visibility clause) without being one of the ten.
 */
const DEFINITION_OF_DONE_COUNT = 10;

export default function CapabilitiesPage() {
  const { scenarios, loading, error, refresh } = useScenarios();
  const { status: backendStatus } = useBackendConfig();
  const { entryFor, activeId, runningAll, runOne, runAll } = useScenarioRunner();

  const availableCount = scenarios?.filter((s) => s.available).length ?? 0;
  const anyRunActive = activeId !== null;

  const tally = useMemo(() => {
    if (!scenarios) return null;
    let passed = 0;
    let failed = 0;
    for (const scenario of scenarios) {
      const entry = entryFor(scenario.id);
      if (entry.status === "passed") passed += 1;
      else if (entry.status === "failed" || entry.status === "connection-error") failed += 1;
    }
    return { passed, failed, notRun: scenarios.length - passed - failed, total: scenarios.length };
  }, [scenarios, entryFor]);

  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      {backendStatus === "offline" && <BackendUnreachableNotice />}
      <Page>
        <PageHeader
          title="Capabilities"
          description={
            <>
              Each scenario drives the real runtime end to end and reports what it found. Nothing
              here is pre-recorded, and a scenario that cannot run says why rather than passing
              vacuously.
            </>
          }
          actions={
            <Button
              onClick={() => scenarios && void runAll(scenarios)}
              disabled={!scenarios || availableCount === 0 || anyRunActive}
            >
              <SquareStackIcon />
              {runningAll ? "Running all…" : `Run all (${availableCount})`}
            </Button>
          }
        />

        {tally && (
          <div className="flex flex-wrap gap-8 rounded-xl border border-border bg-card px-5 py-4">
            <Stat label="Scenarios" value={tally.total} />
            <Stat
              label="Passed"
              value={tally.passed}
              tone={tally.passed > 0 ? "default" : "muted"}
            />
            <Stat
              label="Failed"
              value={tally.failed}
              tone={tally.failed > 0 ? "failed" : "muted"}
            />
            <Stat label="Not yet run" value={tally.notRun} tone="muted" />
          </div>
        )}

        {loading && scenarios === null && (
          <div className="flex flex-col gap-3">
            <Skeleton className="h-28 w-full" />
            <Skeleton className="h-28 w-full" />
            <Skeleton className="h-28 w-full" />
          </div>
        )}

        {error && scenarios === null && !loading && (
          <Alert variant="destructive">
            <AlertTriangleIcon />
            <AlertTitle>Could not reach the backend</AlertTitle>
            <AlertDescription>
              <p>{error}</p>
              <Button size="sm" variant="outline" className="mt-2" onClick={() => void refresh()}>
                <RefreshCwIcon /> Try again
              </Button>
            </AlertDescription>
          </Alert>
        )}

        {scenarios !== null && scenarios.length === 0 && (
          <EmptyState
            icon={FlaskConicalIcon}
            title="No scenarios are registered"
            description="The backend reported an empty suite, which means this build has none compiled in."
          />
        )}

        {scenarios !== null && scenarios.length > 0 && (
          <>
            <ScenarioGroup
              title="Definition of done"
              description="The ten things v1 has to do, verified against the running code."
              scenarios={scenarios.slice(0, DEFINITION_OF_DONE_COUNT)}
              entryFor={entryFor}
              anyRunActive={anyRunActive}
              onRun={runOne}
            />
            <ScenarioGroup
              title="Other capabilities"
              description="Demonstrated alongside the ten, and not counted among them."
              scenarios={scenarios.slice(DEFINITION_OF_DONE_COUNT)}
              entryFor={entryFor}
              anyRunActive={anyRunActive}
              onRun={runOne}
            />
          </>
        )}
      </Page>
    </div>
  );
}

function ScenarioGroup({
  title,
  description,
  scenarios,
  entryFor,
  anyRunActive,
  onRun,
}: {
  title: string;
  description: string;
  scenarios: ScenarioSummary[];
  entryFor: ReturnType<typeof useScenarioRunner>["entryFor"];
  anyRunActive: boolean;
  onRun: (scenario: ScenarioSummary) => void;
}) {
  if (scenarios.length === 0) return null;
  return (
    <Section title={title} description={description}>
      <div className="flex flex-col gap-3">
        {scenarios.map((scenario) => (
          <ScenarioCard
            key={scenario.id}
            scenario={scenario}
            entry={entryFor(scenario.id)}
            disabled={anyRunActive}
            onRun={() => void onRun(scenario)}
          />
        ))}
      </div>
    </Section>
  );
}
