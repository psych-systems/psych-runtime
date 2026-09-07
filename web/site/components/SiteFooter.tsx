import Link from "next/link";
import { TridentTile } from "@/components/Trident";
import { ArrowUpRight } from "@/components/icons";
import { FOOTER, SITE } from "@/lib/site";

export function SiteFooter() {
  return (
    <footer className="footer">
      <div className="shell footer-grid">
        <div className="footer-brand">
          <Link href="/" className="brand" aria-label="Psych Runtime, home">
            <TridentTile size={30} />
            <span className="brand-name">
              Psych <span>Runtime</span>
            </span>
          </Link>
          <p>{SITE.tagline}</p>
        </div>
        <FooterColumn title="Product" items={FOOTER.product} />
        <FooterColumn title="Learn" items={FOOTER.learn} />
        <FooterColumn title="Project" items={FOOTER.project} />
      </div>
      <div className="shell footer-foot">
        <span className="meta">
          {SITE.license} · Python {SITE.python}
        </span>
        <span className="meta">
          Built by {SITE.org}
        </span>
      </div>
    </footer>
  );
}

function FooterColumn({
  title,
  items,
}: {
  title: string;
  items: readonly { href: string; label: string; external?: boolean }[];
}) {
  return (
    <div className="footer-col">
      <h2>{title}</h2>
      <ul>
        {items.map((item) => (
          <li key={item.href}>
            {item.external ? (
              <a href={item.href} rel="noopener">
                {item.label} <ArrowUpRight size={12} aria-hidden />
              </a>
            ) : (
              <Link href={item.href}>{item.label}</Link>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
