import type { LucideIcon } from "lucide-react";
import {
  ActivityIcon,
  BotIcon,
  FlaskConicalIcon,
  HistoryIcon,
  MessagesSquareIcon,
  PlugIcon,
  Settings2Icon,
  WorkflowIcon,
} from "lucide-react";

/**
 * The five places this product has, and the line between them.
 *
 * The old nav was Chat / Agents / Tools / Runs / Capabilities / Settings, and
 * three of those were the runtime's own vocabulary rather than anything a
 * person came to do. "Runs" listed every turn of every conversation as its own
 * row while "Chat" listed the conversations; "Tools" was a read-only registry
 * dump with a policy preview that saved nothing.
 *
 * What replaced them:
 *
 * - **Chat** is the product. One conversation at a time, nothing technical.
 * - **Conversations** is everything you have asked, in plain terms. The same
 *   list the chat panel shows, reachable on its own so it survives the panel
 *   being collapsed and is still one icon away when the nav is.
 * - **Agents** is what you can talk to, and how to make another.
 * - **Connections** is what those agents can reach: MCP servers, their live
 *   state, and the credentials they use. Previously buried in a settings tab,
 *   which is why a connected server did not look connected after a refresh.
 * - **Activity** is every conversation with its outcome, and the one door to
 *   the technical view of a single run: its trace and its usage.
 * - **Settings** is model access, secrets and appearance.
 *
 * `DEVELOPER_ITEMS` is a second, visually separated group. The capability
 * suite is genuinely for someone evaluating the runtime rather than using it,
 * and putting it beside Chat implied otherwise.
 */
export interface NavItem {
  title: string;
  href: string;
  icon: LucideIcon;
  description: string;
}

export const NAV_ITEMS: NavItem[] = [
  { title: "Chat", href: "/chat", icon: MessagesSquareIcon, description: "Talk to an agent" },
  {
    title: "Conversations",
    href: "/conversations",
    icon: HistoryIcon,
    description: "Everything you have asked",
  },
  { title: "Agents", href: "/agents", icon: BotIcon, description: "What you can talk to" },
  {
    title: "Workflows",
    href: "/workflows",
    icon: WorkflowIcon,
    description: "Fixed pipelines of steps",
  },
  {
    title: "Connections",
    href: "/connections",
    icon: PlugIcon,
    description: "Systems your agents can reach",
  },
  { title: "Activity", href: "/activity", icon: ActivityIcon, description: "Every conversation" },
  { title: "Settings", href: "/settings", icon: Settings2Icon, description: "Models and secrets" },
];

export const DEVELOPER_ITEMS: NavItem[] = [
  {
    title: "Capabilities",
    href: "/capabilities",
    icon: FlaskConicalIcon,
    description: "Prove the runtime does what it claims",
  },
];

const ALL_ITEMS = [...NAV_ITEMS, ...DEVELOPER_ITEMS];

/** The nav item active for `pathname`: the longest `href` that prefixes it,
 * so `/activity/run_x` highlights Activity and `/chat/run_x` highlights Chat. */
export function activeNavItem(pathname: string): NavItem | null {
  let best: NavItem | null = null;
  for (const item of ALL_ITEMS) {
    const matches = pathname === item.href || pathname.startsWith(`${item.href}/`);
    if (matches && (best === null || item.href.length > best.href.length)) {
      best = item;
    }
  }
  return best;
}
