# web

One site, one build, one source of truth for its content.

`site/` is [psychruntime.com](https://psychruntime.com): the marketing pages and
the documentation under `/docs`, as a single Next.js app that exports to plain
HTML. It deploys to Cloudflare as static assets, so there is no server to be
slow, run out of memory, or serve a page the build never produced.

They are one deployment rather than two because the two halves share a design
system, a header, the Trident and every fact about the package, and because a
reader moving from a capability to the guide that explains it should not change
origin to do it. `DEPLOY.md` has the commands.

## Working on it

```sh
cd web && pnpm install
pnpm dev            # http://localhost:3001
pnpm check          # tsc --noEmit
pnpm build          # static export into site/out
pnpm preview        # serve site/out under the real Cloudflare runtime
```

## Where the content comes from

**Nothing under `site/content/docs/next/` that the sync writes is authored
here.** The library is the source and `pnpm sync` copies from it:

| Docs page | Copied from |
|---|---|
| `changelog.mdx` | `CHANGELOG.md` |
| `reference/api.mdx` | `docs/api.md` |
| `design/*.mdx` | `docs/design-notes/*.md` |
| `guides/*.mdx` | `.agents/skills/*/SKILL.md` (26 of them) |

`reference/*` other than `api.mdx` is generated from `psych_runtime.__all__` by
`scripts/generate_docs.py` in the repository root.

Editing a synced page here is a mistake the sync will silently undo. Edit the
file in the repository root and run `pnpm sync`. Pages under
`site/content/docs/next/` that are *not* in that table are written here and
owned here: `index`, `quickstart`, `concepts/*`, and every `meta.json`, which
carries reading order rather than content.

The skills are published as guides rather than rewritten for humans. They were
written for a coding agent and read perfectly well to a person: one feature, the
working code, the gotchas that bite. A second set of feature guides written by
hand would be the same content maintained twice, and the second copy is always
the stale one. Only their frontmatter is replaced, because a skill's description
is tuned for retrieval and reads as noise under a heading.

`scripts/sync-from-repo.mjs` fails when a source file has moved rather than
writing a stale copy, so a rename in the library breaks the build instead of
quietly leaving last month's page up.

## The marketing side

| Where | What |
|---|---|
| `site/app/(site)/` | The pages: home, runtime, capabilities, examples, integrations, playground, open-source, changelog, about. |
| `site/content/site/` | Every fact they state about the package: the code samples, the capability list, the integrations list. |
| `site/lib/site.ts` | The name, the URL, the install command, the navigation. |
| `site/components/` | The primitives: Trident, Code, InstallCommand, SiteNav, SiteFooter, RunStepper, LifecycleRail, Reveal. |
| `site/app/global.css` | The design tokens and both surfaces, marketing and fumadocs. |
| `site/app/opengraph-image.tsx` | The social card, rendered at build time from the Trident and the palette. |
| `site/scripts/name-images.mjs` | Gives the generated images a `.png` extension, which a static host needs to serve them as images. Runs as part of `build`. |

Content lives apart from layout on purpose. `scripts/check-site.mjs` reads
`content/site/` and `lib/site.ts` and fails the gate when a sample calls a
`psych_runtime` symbol that is no longer exported, or when the install command
stops naming the real distribution. Those are the strings a visitor copies
first, and they went stale here once already.

## Versioning

`content/docs/next/` tracks `main`. `pnpm cut 0.2.0` freezes a copy of it as
`content/docs/v0.2/` and points `latest` at it.

Released documentation is versioned by minor release so patch releases can
correct the matching documentation in place.
