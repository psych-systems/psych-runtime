"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useId, useRef, useState } from "react";
import { TridentTile } from "@/components/Trident";
import { ThemeToggle } from "@/components/ThemeToggle";
import { ChevronDown, ICON, Menu, X } from "@/components/icons";
import { NAV, SITE, type NavItem } from "@/lib/site";

function isActive(pathname: string, href: string) {
  return pathname === href || pathname.startsWith(`${href}/`);
}

export function SiteNav() {
  const pathname = usePathname();
  const [open, setOpen] = useState(false);
  const menuId = useId();

  useEffect(() => setOpen(false), [pathname]);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  return (
    <header className="nav">
      <div className="shell nav-row">
        <Link href="/" className="brand" aria-label="Psych Runtime, home">
          <TridentTile size={30} />
          <span className="brand-name">
            Psych <span>Runtime</span>
          </span>
        </Link>

        <nav id={menuId} className="nav-links" data-open={open} aria-label="Primary">
          {NAV.map((item) =>
            item.children ? (
              <NavMenu key={item.label} item={item} pathname={pathname} />
            ) : (
              <Link
                key={item.href}
                href={item.href}
                aria-current={isActive(pathname, item.href) ? "page" : undefined}
              >
                {item.label}
              </Link>
            ),
          )}
        </nav>

        <div className="nav-end">
          <ThemeToggle />
          <a className="btn btn-ghost btn-sm nav-github" href={SITE.github} rel="noopener">
            GitHub
          </a>
          <button
            type="button"
            className="icon-btn nav-toggle"
            aria-expanded={open}
            aria-controls={menuId}
            aria-label={open ? "Close menu" : "Open menu"}
            onClick={() => setOpen((v) => !v)}
          >
            {open ? <X size={ICON} aria-hidden /> : <Menu size={ICON} aria-hidden />}
          </button>
        </div>
      </div>
    </header>
  );
}

/**
 * One group of pages behind one label.
 *
 * On a wide screen this is a button that opens a panel: Escape closes it, and
 * so does moving focus or the pointer out of the group, which is what makes it
 * usable without trapping anyone in it. Below the breakpoint the whole header
 * is already a vertical stack, so the panel is not a panel at all -- the group
 * renders inline under a heading, and there is nothing extra to open.
 */
function NavMenu({ item, pathname }: { item: NavItem; pathname: string }) {
  // Two reasons the panel can be open, kept apart on purpose. Hovering opens
  // it and leaving closes it, which is what a pointer user expects. Clicking
  // *pins* it, which is what a keyboard or touch user needs, and pinning is a
  // separate flag because a plain toggle closes the panel the hover just
  // opened: the pointer arrives before the click and the click undoes it.
  const [hover, setHover] = useState(false);
  const [pinned, setPinned] = useState(false);
  const open = hover || pinned;
  const panelId = useId();
  const wrap = useRef<HTMLDivElement>(null);
  const children = item.children ?? [];
  const active = children.some((c) => isActive(pathname, c.href));

  const close = () => {
    setHover(false);
    setPinned(false);
  };

  useEffect(close, [pathname]);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    const onDown = (e: PointerEvent) => {
      if (!wrap.current?.contains(e.target as Node)) close();
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("pointerdown", onDown);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("pointerdown", onDown);
    };
  }, [open]);

  return (
    <div
      className="nav-menu"
      ref={wrap}
      data-open={open}
      onPointerEnter={() => setHover(true)}
      onPointerLeave={() => setHover(false)}
      onBlur={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node)) close();
      }}
    >
      <button
        type="button"
        className="nav-menu-btn"
        aria-expanded={open}
        aria-controls={panelId}
        aria-current={active ? "true" : undefined}
        onClick={() => setPinned((v) => !v)}
      >
        {item.label}
        <ChevronDown size={14} aria-hidden />
      </button>
      <span className="nav-group-label" aria-hidden>
        {item.label}
      </span>
      <div id={panelId} className="nav-panel" role="group" aria-label={item.label}>
        {children.map((c) => (
          <Link
            key={c.href}
            href={c.href}
            aria-current={isActive(pathname, c.href) ? "page" : undefined}
          >
            <strong>{c.label}</strong>
            <span>{c.blurb}</span>
          </Link>
        ))}
      </div>
    </div>
  );
}
