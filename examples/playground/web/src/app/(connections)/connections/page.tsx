import { ConnectionsView } from "@/components/connections/connections-view";

export const metadata = {
  title: "Connections",
};

export default function ConnectionsPage() {
  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
      <ConnectionsView />
    </div>
  );
}
