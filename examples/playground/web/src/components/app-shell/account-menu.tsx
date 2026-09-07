"use client";

import { LogOutIcon } from "lucide-react";
import { toast } from "sonner";

import { useSession } from "@/components/auth/session-provider";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { SidebarMenuButton } from "@/components/ui/sidebar";
import { describeApiError } from "@/lib/errors";

/**
 * Who you are, at the bottom of the nav.
 *
 * Collapsed to the rail this is the initial in a circle, on the same 24px
 * centre line as every other icon; expanded it is the name over the address.
 * The address is behind the menu rather than in the sidebar because a console
 * left open on a shared screen should not display somebody's email all day,
 * and it is *in* the menu because a person with two accounts needs to know
 * which one they are looking at.
 */
export function AccountMenu({ collapsed }: { collapsed: boolean }) {
  const { account, signOut } = useSession();
  if (account === null) return null;

  const initial = (account.display_name || account.email).charAt(0).toUpperCase();

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <SidebarMenuButton
          aria-label={`Signed in as ${account.display_name}`}
          tooltip={account.display_name}
          className="h-auto py-1.5"
        >
          <span
            aria-hidden
            className="flex size-5 shrink-0 items-center justify-center rounded-full bg-surface text-micro font-medium text-surface-foreground"
          >
            {initial}
          </span>
          {!collapsed && <span className="truncate">{account.display_name}</span>}
        </SidebarMenuButton>
      </DropdownMenuTrigger>
      <DropdownMenuContent side="right" align="end" className="w-56">
        <DropdownMenuLabel className="flex flex-col gap-0.5 font-normal">
          <span className="font-medium">{account.display_name}</span>
          <span className="text-micro break-all text-muted-foreground">{account.email}</span>
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          onClick={async () => {
            try {
              await signOut();
            } catch (err) {
              // Signing out is idempotent on the backend, so a failure here is
              // the network rather than the session. Saying so beats leaving
              // someone on a page that looks signed in.
              toast.error("Could not sign out", { description: describeApiError(err) });
            }
          }}
        >
          <LogOutIcon />
          Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
