import { TraceView } from "@/components/trace/trace-view";
import { ReviewTraceDetail } from "@/components/activity/review-diagnostics";

export default async function ActivityRunTracePage(props: PageProps<"/activity/[runId]/trace">) {
  const { runId } = await props.params;
  const query = await props.searchParams;
  if (process.env.NODE_ENV === "development" && query.review === "1") {
    return <ReviewTraceDetail />;
  }
  return <TraceView runId={runId} />;
}
