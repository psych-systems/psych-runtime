import { RunDetail } from "@/components/activity/run-detail";

export default async function ActivityRunPage(props: PageProps<"/activity/[runId]">) {
  const { runId } = await props.params;
  return <RunDetail runId={runId} />;
}
