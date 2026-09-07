import { TraceView } from "@/components/trace/trace-view";

export default async function ActivityRunTracePage(props: PageProps<"/activity/[runId]/trace">) {
  const { runId } = await props.params;
  return <TraceView runId={runId} />;
}
