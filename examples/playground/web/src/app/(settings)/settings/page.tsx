"use client";

import { useEffect, useState } from "react";

import Link from "next/link";
import { AlertTriangleIcon, PlugIcon } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Page, PageHeader } from "@/components/ui/page";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { useSettings } from "@/components/settings/use-settings";
import { AppearanceSection } from "@/components/settings/appearance-section";
import { MemorySection } from "@/components/settings/memory-section";
import { ModelAccessSection } from "@/components/settings/model-access-section";
import { PricingSection } from "@/components/settings/pricing-section";
import { RuntimeSection } from "@/components/settings/runtime-section";
import { listModels } from "@/lib/api";
import { SecretsSection } from "@/components/settings/secrets-section";
import { SkillLibrarySection } from "@/components/settings/skill-library-section";

/**
 * Sections on one scrolling page, deliberately not tabs.
 *
 * Radix tabs unmount what they hide, and everything worth keeping on this page
 * is the result of an action: whether a key worked, whether a secret saved. A
 * tab switch threw all of it away, so a person testing a provider and then
 * checking a secret came back to a page that had forgotten the test.
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
          description="Model access, the skills your agents can be given, the secrets your connections use, and how this app looks."
        />

        {/* Shaped like the three sections it becomes, so the page settles
            into place rather than growing under whoever is reading it. */}
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
          <div className="flex flex-col gap-8">
            <ModelAccessSection
              providers={settings.providers}
              activeProviderId={settings.active_provider_id}
              onSave={saveProvider}
              onDelete={removeProvider}
              onActivate={activate}
              onTest={runTest}
            />

            <Separator />

            <SkillLibrarySection skills={settings.skills} onSave={saveSkills} />

            <Separator />

            <MemorySection />

            <Separator />

            <PricingSection
              prices={settings.model_prices}
              models={models}
              onSave={savePrices}
            />

            <Separator />

            <RuntimeSection runtime={settings.runtime} onSave={saveRuntime} />

            <Separator />

            <SecretsSection
              secrets={settings.secrets}
              onAdd={saveSecret}
              onRemove={removeSecret}
            />

            <Separator />

            <AppearanceSection />

            <p className="text-caption text-muted-foreground">
              Looking for the systems your agents can reach?{" "}
              <Link href="/connections" className="font-medium text-primary underline underline-offset-2">
                They live under Connections.
              </Link>
            </p>
          </div>
        )}
    </Page>
  );
}
