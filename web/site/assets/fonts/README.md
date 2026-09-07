# Fonts for the social image

`app/opengraph-image.tsx` and its siblings render a PNG at build time, and the
renderer needs a real font file rather than a stylesheet. These two are read
from disk so the build never depends on a font CDN being reachable, which is
exactly what a static export exists to survive.

The pages themselves do not use these files: `next/font/google` downloads and
self-hosts the variable faces at build time.

| File | Face | Licence |
|---|---|---|
| `fraunces-500.woff` | Fraunces, weight 500 | SIL Open Font License 1.1 |
| `plex-mono-400.woff` | IBM Plex Mono, weight 400 | SIL Open Font License 1.1 |

To refresh one, take the URL from the Google Fonts CSS API with a user agent
old enough to be served woff rather than woff2.
