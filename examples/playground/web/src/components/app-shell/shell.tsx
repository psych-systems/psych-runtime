"use client";

import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

import { AppSidebar } from "@/components/app-shell/app-sidebar";
import { BackendConfigProvider } from "@/components/app-shell/backend-config-provider";
import { SessionProvider, useSession } from "@/components/auth/session-provider";
import { SidebarInset, SidebarProvider, SidebarTrigger } from "@/components/ui/sidebar";
import { TooltipProvider } from "@/components/ui/tooltip";

const DOOR_ROUTES = ["/sign-in", "/sign-up"];

/**
 * The frame every page sits in.
 *
 * There is deliberately no header bar here any more. The old one was a fixed
 * 48px strip reading "Agent runtime playground" on every screen, stacked above
 * each page's own header: two headers per page, one of which never changed and
 * told nobody anything. Each page owns its own title through
 * `components/ui/page.tsx`, and the sidebar trigger floats over the content
 * for the narrow widths where the sidebar collapses.
 */
export function AppShell({ children }: { children: ReactNode }) {
  return (
    <SessionProvider>
      <BackendConfigProvider>
        <TooltipProvider delayDuration={150}>
          <Frame>{children}</Frame>
        </TooltipProvider>
      </BackendConfigProvider>
    </SessionProvider>
  );
}

/**
 * The nav, or nothing at all.
 *
 * Sign-in and sign-up render on a bare page. A door with the whole application
 * already drawn behind it is a door that looks optional, and every nav item
 * behind it would lead somewhere that bounces straight back here.
 *
 * The blank frame while the session resolves is deliberate too. It is one
 * request and usually invisible, and the alternative is worse: painting the
 * console for a beat and then replacing it with a sign-in form reads as the
 * application crashing.
 */
function Frame({ children }: { children: ReactNode }) {
  const { status } = useSession();
  const pathname = usePathname();
  const atTheDoor = DOOR_ROUTES.includes(pathname);

  if (status === "loading") return <div className="flex-1" />;

  // The door renders bare: a sign-in form with the whole console already drawn
  // behind it looks optional, and every nav item behind it leads somewhere
  // that bounces straight back.
  if (atTheDoor) return <main className="flex min-h-0 flex-1 flex-col">{children}</main>;

  // Anonymous anywhere else renders *nothing*, rather than rendering the page
  // it is about to be redirected away from. Rendering it mounts every data
  // hook on it, each of which fires a request that can only 401 -- a dozen
  // console errors and a dozen pointless round trips, on the way to a screen
  // nobody will see. `SessionProvider` is already navigating.
  if (status === "anonymous") return <div className="flex-1" />;

  // `unreachable` still gets the full shell: the health indicator in the
  // sidebar footer is the thing that explains what is wrong, and hiding it
  // behind a sign-in form nobody can submit would be the least helpful
  // possible response to a backend being down.

  return (
    <SidebarProvider>
      <AppSidebar />
      {/* `overflow-hidden` is what stops a second scrollbar appearing on the
          document behind each page's own scrolling pane.

          Every page here owns its scrolling: a `flex-1 overflow-y-auto` pane
          that fills the viewport. On the agent form that pane held 4063px of
          content in a 950px viewport and the *document* was also 47px too
          tall, so the whole console -- sidebar included -- shifted up by 47px
          when you scrolled past the end. This element is `relative`, so a
          descendant positioned against it can reach past its box and count
          toward an ancestor's scroll height without making any layout box
          bigger, which is why nothing measured as overflowing and why
          `min-height`, `height` and `overflow` on the wrapper and on `body`
          all changed nothing. Clipping here is the fix, and it hides nothing:
          the pane inside it scrolls, and menus and dialogs render in portals
          at the body.

          `min-w-0` is the horizontal twin, added earlier for the same class of
          bug. */}
      <SidebarInset className="min-w-0 overflow-hidden">
        <SidebarTrigger className="absolute top-3 left-3 z-20 md:hidden" />
        <div className="flex min-h-0 flex-1 flex-col">{children}</div>
      </SidebarInset>
    </SidebarProvider>
  );
}
