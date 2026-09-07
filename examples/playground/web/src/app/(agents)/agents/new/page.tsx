import { Suspense } from "react";

import { CreateAgentForm } from "@/components/agents/create-agent-form";
import { Page, PageHeader } from "@/components/ui/page";
import { Skeleton } from "@/components/ui/skeleton";

export default function NewAgentPage() {
  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
      <Page className="max-w-3xl">
        <PageHeader
          title="New agent"
          description="Everything is checked when you publish, so nothing here can go wrong later in front of someone waiting for an answer."
        />
        {/* The form reads `?from=` to prefill a duplicate, and
            `useSearchParams` opts a route out of prerendering unless the
            component using it sits behind a Suspense boundary. Scoped to the
            form so the page's own header still renders statically. */}
        <Suspense fallback={<Skeleton className="h-96 w-full rounded-xl" />}>
          <CreateAgentForm />
        </Suspense>
      </Page>
    </div>
  );
}
