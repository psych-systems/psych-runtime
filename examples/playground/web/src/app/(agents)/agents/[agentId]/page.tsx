import { AgentDetail } from "@/components/agents/agent-detail";

export default async function AgentDetailPage({ params }: PageProps<"/agents/[agentId]">) {
  const { agentId } = await params;
  // No decoding needed, unlike the version hash this segment used to carry: an
  // agent id is hex, so nothing in it is reserved in a path. That is a small
  // part of why an agent has an id of its own rather than being addressed by
  // whatever it happens to run today.
  return <AgentDetail agentId={agentId} />;
}
