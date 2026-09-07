"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { PanelLeftCloseIcon, PanelLeftOpenIcon } from "lucide-react";

import { AccountMenu } from "@/components/app-shell/account-menu";
import { activeNavItem, DEVELOPER_ITEMS, NAV_ITEMS } from "@/components/app-shell/nav-items";
import { cn } from "@/lib/utils";
import { HealthIndicator } from "@/components/app-shell/health-indicator";
import { ThemeToggle } from "@/components/app-shell/theme-toggle";
import { Trident } from "@/components/brand/trident";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarSeparator,
  useSidebar,
} from "@/components/ui/sidebar";

export function AppSidebar() {
  const pathname = usePathname();
  const active = activeNavItem(pathname);
  const { state, toggleSidebar } = useSidebar();
  const collapsed = state === "collapsed";

  return (
    <Sidebar collapsible="icon">
      <SidebarHeader className="gap-0 px-2 pt-3 pb-2">
        {/* Collapsed, this glyph is the top of a column of icons and has to
            sit on their centre line. A menu button is 32px inside the group's
            own 8px padding, so its icon centres at 24px from the rail's edge;
            the same 8px header padding centres a 28px glyph there only if the
            link stops adding its own. Expanded, the padding is what keeps the
            wordmark off the edge, so it comes back. */}
        <Link
          href="/chat"
          className={cn(
            "flex items-center rounded-md py-1 outline-hidden focus-visible:ring-2 focus-visible:ring-ring",
            collapsed ? "size-8 justify-center px-0" : "gap-2.5 px-1.5",
          )}
        >
          <span
            aria-hidden
            className="flex size-7 shrink-0 items-center justify-center rounded-lg bg-primary font-heading text-glyph font-semibold text-primary-foreground"
          >
            <Trident className="size-5" />
          </span>
          {!collapsed && (
            <span className="flex min-w-0 flex-col">
              <span className="font-heading text-mark font-semibold tracking-tight text-sidebar-foreground">
                Psych
              </span>
              <span className="text-micro leading-tight text-muted-foreground">
                Agent platform
              </span>
            </span>
          )}
        </Link>
      </SidebarHeader>

      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupContent>
            <SidebarMenu>
              {NAV_ITEMS.map((item) => (
                <SidebarMenuItem key={item.href}>
                  <SidebarMenuButton
                    asChild
                    isActive={active?.href === item.href}
                    tooltip={item.title}
                  >
                    <Link href={item.href}>
                      <item.icon />
                      <span>{item.title}</span>
                    </Link>
                  </SidebarMenuButton>
                </SidebarMenuItem>
              ))}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>

        {/* Separated, and labelled for who it is for. The capability suite
            proves things about the runtime rather than doing anything with it,
            and sitting beside Chat implied otherwise. */}
        <SidebarGroup className="mt-auto">
          {!collapsed && <SidebarGroupLabel>For developers</SidebarGroupLabel>}
          <SidebarGroupContent>
            <SidebarMenu>
              {DEVELOPER_ITEMS.map((item) => (
                <SidebarMenuItem key={item.href}>
                  <SidebarMenuButton
                    asChild
                    isActive={active?.href === item.href}
                    tooltip={item.title}
                  >
                    <Link href={item.href}>
                      <item.icon />
                      <span>{item.title}</span>
                    </Link>
                  </SidebarMenuButton>
                </SidebarMenuItem>
              ))}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>

      <SidebarFooter className="gap-1 px-2 pb-3">
        <SidebarSeparator className="mx-0 mb-1" />
        {/* The collapse control lives here, not floating over the page. It was
            `md:hidden`, so on the width where the icon rail is actually useful
            there was no way to reach it: the sidebar could collapse and nobody
            could ask it to. */}
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton
              onClick={toggleSidebar}
              tooltip={collapsed ? "Expand" : "Collapse"}
              aria-label={collapsed ? "Expand the sidebar" : "Collapse the sidebar"}
              className="hidden md:flex"
            >
              {collapsed ? <PanelLeftOpenIcon /> : <PanelLeftCloseIcon />}
              <span>Collapse</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
        <SidebarMenu>
          <SidebarMenuItem>
            <AccountMenu collapsed={collapsed} />
          </SidebarMenuItem>
        </SidebarMenu>
        <ThemeToggle collapsed={collapsed} />
        <HealthIndicator collapsed={collapsed} />
      </SidebarFooter>
    </Sidebar>
  );
}
