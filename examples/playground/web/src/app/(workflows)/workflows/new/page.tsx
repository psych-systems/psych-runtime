import { Suspense } from "react";

import { WorkflowForm } from "@/components/workflows/workflow-form";
import { Page, PageHeader } from "@/components/ui/page";
import { Skeleton } from "@/components/ui/skeleton";

export default function NewWorkflowPage() {
  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
      <Page className="max-w-3xl">
        <PageHeader
          title="New workflow"
          description="Steps in the order you choose. Everything is checked when you publish."
        />
        <Suspense fallback={<Skeleton className="h-96 w-full rounded-xl" />}>
          <WorkflowForm />
        </Suspense>
      </Page>
    </div>
  );
}
