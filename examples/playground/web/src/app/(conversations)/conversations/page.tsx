import { ConversationsView } from "@/components/chat/conversations-view";

export default async function ConversationsPage({
  searchParams,
}: {
  searchParams: Promise<{ review?: string | string[] }>;
}) {
  const params = await searchParams;
  const review = process.env.NODE_ENV === "development" && params.review === "1";
  return <ConversationsView reviewNow={review ? new Date().toISOString() : null} />;
}
