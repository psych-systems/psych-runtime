/**
 * The facts about the site and the project that more than one page states.
 *
 * Anything that appears in metadata, in the footer and in structured data is
 * read from here so it cannot be spelt three ways. The install command and the
 * import name are checked against the Python package by
 * `web/scripts/check-site.mjs`.
 */
import { SITE_URL } from "@/lib/env";
import { docs } from "@/lib/versions";

export const SITE = {
  name: "Psych Runtime",
  /** The origin this build is served from. Production unless PSYCH_SITE_ENV says preview. */
  url: SITE_URL,
  description:
    "Build AI agents and multi-step workflows inside your Python application. Psych runs tools, saves progress, pauses for people, and records what happened.",
  tagline: "Build agents that finish the work.",
  github: "https://github.com/psych-systems/psych-runtime",
  pypi: "https://pypi.org/project/psych-runtime/",
  distribution: "psych-runtime",
  importName: "psych_runtime",
  command: "psych",
  install: "pip install psych-runtime",
  license: "Apache-2.0",
  python: ">=3.12",
  org: "psych-systems",
} as const;

/**
 * The header. Four choices, not six.
 *
 * Runtime, Capabilities and Integrations answer one question between them,
 * "what is this and does it fit", and putting them side by side with Docs
 * made a visitor rank six things before reading anything. They live under one
 * menu now. Examples, Demo and Docs stay one click away because each is
 * somewhere a visitor goes to *do* something. "Demo" rather than "Playground"
 * because the page is a browser simulation first, and the word says so.
 */
export type NavItem = {
  readonly href: string;
  readonly label: string;
  /** A menu instead of a link. `href` is where the label itself goes. */
  readonly children?: readonly { readonly href: string; readonly label: string; readonly blurb: string }[];
};

export const NAV: readonly NavItem[] = [
  {
    href: "/runtime",
    label: "Product",
    children: [
      { href: "/runtime", label: "The runtime", blurb: "How agents execute, pause, and recover." },
      { href: "/capabilities", label: "Capabilities", blurb: "Agents, workflows, memory, tools, and control." },
      { href: "/integrations", label: "Integrations", blurb: "Connect your models, tools, and storage." },
    ],
  },
  { href: "/examples", label: "Examples" },
  { href: "/playground", label: "Demo" },
  { href: "/docs", label: "Docs" },
] as const;

export const FOOTER = {
  product: [
    { href: "/runtime", label: "The runtime" },
    { href: "/capabilities", label: "Capabilities" },
    { href: "/integrations", label: "Integrations" },
    { href: "/playground", label: "Demo" },
    { href: "/examples", label: "Examples" },
  ],
  learn: [
    { href: "/docs", label: "Documentation" },
    { href: docs("get-started"), label: "Get started" },
    { href: docs("reference/api"), label: "The public API" },
    { href: docs("design"), label: "Design notes" },
    { href: "/changelog", label: "Changelog" },
  ],
  project: [
    { href: "/open-source", label: "Open source" },
    { href: "/about", label: "About" },
    { href: SITE.github, label: "GitHub", external: true },
    { href: SITE.pypi, label: "PyPI", external: true },
    { href: `${SITE.github}/issues`, label: "Issues", external: true },
  ],
} as const;
