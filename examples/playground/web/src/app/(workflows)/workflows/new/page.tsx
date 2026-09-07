import { Suspense } from "react";

import { WorkflowForm } from "@/components/workflows/workflow-form";
import { Page, PageHeader } from "@/components/ui/page";
import { Skeleton } from "@/components/ui/skeleton";

export default function NewWorkflowPage() {
  return (
    <Page className="max-w-3xl">
        <PageHeader
          title="New workflow"
          description="Steps in the order you choose. Everything is checked when you publish."
        />
        <Suspense fallback={<Skeleton className="h-96 w-full rounded-xl" />}>
          <WorkflowForm />
        </Suspense>
    </Page>
  );
}
