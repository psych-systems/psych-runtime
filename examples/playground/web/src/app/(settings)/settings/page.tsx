"use client";

import { useEffect, useState } from "react";

import Link from "next/link";
import { AlertTriangleIcon, PlugIcon } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { HelpTip } from "@/components/ui/help";
import { Page, PageHeader } from "@/components/ui/page";
import { Skeleton } from "@/components/ui/skeleton";
import { useSettings } from "@/components/settings/use-settings";
import { AppearanceSection } from "@/components/settings/appearance-section";
import { MemorySection } from "@/components/settings/memory-section";
import { PricingSection } from "@/components/settings/pricing-section";
import { ProvidersSection } from "@/components/settings/providers-section";
import { RuntimeSection } from "@/components/settings/runtime-section";
import { SandboxSection } from "@/components/settings/sandbox-section";
import { SETTINGS_SECTIONS, SectionNav } from "@/components/settings/section-nav";
import { listModels } from "@/lib/api";
import { SecretsSection } from "@/components/settings/secrets-section";
import { SkillLibrarySection } from "@/components/settings/skill-library-section";

/**
 * One page, eight sections, a sticky strip that moves between them.
 *
 * Anchors, deliberately not tabs. Radix tabs unmount what they hide, and
 * everything worth keeping on this page is the result of an action: whether a
 * key worked, what a sandbox probe found. A tab switch threw all of it away,
 * so a person testing a provider and then checking a secret came back to a
 * page that had forgotten the test.
 *
 * Every explanation lives behind a "?" beside the thing it explains. Someone
 * who knows what they want changes a setting in seconds; someone who does not
 * is one hover away.
 *
 * Connections moved out entirely, onto their own page. They are not settings.
 */
export default function SettingsPage() {
  const {
    settings,
    loading,
    error,
    refresh,
    saveProvider,
    removeProvider,
    activate,
    runTest,
    saveSecret,
    removeSecret,
    savePrices,
    saveSkills,
    saveRuntime,
    checkSandbox,
  } = useSettings();

  // For the rate rows' model picker. Best-effort: a provider that will not
  // list its models, or is down, leaves the field typeable rather than
  // blocking somebody from entering a rate they already know.
  const [models, setModels] = useState<string[]>([]);
  useEffect(() => {
    void listModels()
      .then((response) => setModels(response.models))
      .catch(() => undefined);
  }, []);

  return (
    <Page>
      <PageHeader
        title="Settings"
        description={
          <span className="inline-flex flex-wrap items-center gap-1.5">
            How this installation runs, in one place.
            <HelpTip title="Settings" short="Installation, not agents.">
              <p>
                Nothing on this page is part of a published agent, so changing it moves no version
                hash and republishes nothing. It changes how the next message runs.
              </p>
              <p>
                The systems your agents can reach live under Connections, because they are
                something you come back to rather than set once.
              </p>
            </HelpTip>
          </span>
        }
      />

      {settings !== null && <SectionNav sections={SETTINGS_SECTIONS} />}

      {/* Shaped like the sections it becomes, so the page settles into place
          rather than growing under whoever is reading it. */}
      {loading && settings === null && (
        <div className="flex flex-col gap-8">
          <div className="flex flex-col gap-3">
            <Skeleton className="h-6 w-40" />
            <Skeleton className="h-32 w-full rounded-xl" />
          </div>
          <div className="flex flex-col gap-3">
            <Skeleton className="h-6 w-28" />
            <Skeleton className="h-24 w-full rounded-xl" />
          </div>
          <div className="flex flex-col gap-3">
            <Skeleton className="h-6 w-36" />
            <Skeleton className="h-16 w-full rounded-xl" />
          </div>
        </div>
      )}

      {error && settings === null && !loading && (
        <Alert variant="destructive">
          <AlertTriangleIcon />
          <AlertTitle>Couldn&apos;t reach the backend</AlertTitle>
          <AlertDescription>
            <p>{error}</p>
            <Button size="sm" variant="outline" className="mt-2" onClick={() => void refresh()}>
              <PlugIcon /> Try again
            </Button>
          </AlertDescription>
        </Alert>
      )}

      {settings !== null && (
        <div className="flex flex-col gap-10">
          <ProvidersSection
            providers={settings.providers}
            activeProviderId={settings.active_provider_id}
            onSave={saveProvider}
            onDelete={removeProvider}
            onActivate={activate}
            onTest={runTest}
          />

          <RuntimeSection runtime={settings.runtime} onSave={saveRuntime} />

          <SandboxSection
            runtime={settings.runtime}
            secrets={settings.secrets}
            onSave={saveRuntime}
            onCheck={checkSandbox}
          />

          <SecretsSection
            secrets={settings.secrets}
            onAdd={saveSecret}
            onRemove={removeSecret}
          />

          <SkillLibrarySection skills={settings.skills} onSave={saveSkills} />

          <PricingSection prices={settings.model_prices} models={models} onSave={savePrices} />

          <MemorySection />

          <AppearanceSection />

          <p className="text-caption text-muted-foreground">
            Looking for the systems your agents can reach?{" "}
            <Link
              href="/connections"
              className="font-medium text-primary underline underline-offset-2"
            >
              They live under Connections.
            </Link>
          </p>
        </div>
      )}
    </Page>
  );
}
