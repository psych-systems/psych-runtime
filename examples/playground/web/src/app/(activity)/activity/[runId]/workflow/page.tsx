import { WorkflowRunView } from "@/components/workflows/workflow-run-view";

export default async function ActivityRunWorkflowPage(
  props: PageProps<"/activity/[runId]/workflow">,
) {
  const { runId } = await props.params;
  return <WorkflowRunView runId={runId} />;
}
