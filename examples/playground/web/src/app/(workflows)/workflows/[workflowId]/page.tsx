import { WorkflowDetail } from "@/components/workflows/workflow-detail";

export default async function WorkflowDetailPage({ params }: PageProps<"/workflows/[workflowId]">) {
  const { workflowId } = await params;
  return <WorkflowDetail workflowId={workflowId} />;
}
