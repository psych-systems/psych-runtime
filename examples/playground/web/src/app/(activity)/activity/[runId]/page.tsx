import { RunDetail } from "@/components/activity/run-detail";
import { ReviewRunDetail } from "@/components/activity/review-diagnostics";

export default async function ActivityRunPage(props: PageProps<"/activity/[runId]">) {
  const { runId } = await props.params;
  const query = await props.searchParams;
  if (process.env.NODE_ENV === "development" && query.review === "1") {
    return <ReviewRunDetail />;
  }
  return <RunDetail runId={runId} />;
}
