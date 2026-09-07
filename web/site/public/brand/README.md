# Psych Runtime brand assets

## The mark

The Trident: the letter psi drawn as a system glyph, three prongs meeting one
stem. Stroke-based, so one drawing scales from a favicon to a poster.

| File | Use |
|---|---|
| `trident.svg` | `currentColor` strokes. Drop it on any surface and it takes the text colour. |
| `trident-copper.svg` | Copper strokes, for surfaces where the colour must be fixed. |
| `../../app/icon.svg` | The favicon: the mark in paper on a copper tile. |
| `../../components/Trident.tsx` | The same paths as a React component, and the labelled diagram. |

The three prongs stand for the three ports a consumer supplies (model, tools,
store) and the stem for the record log they fold into. Do not stretch it,
rotate it, add a gradient to it, or fill the cup. Give it clear space of at
least half its height on every side, and do not set it smaller than 16px.

## The wordmark

There is no separate wordmark file. The name is set in IBM Plex Sans, weight
600, as two words: "Psych" in the text colour and "Runtime" in the muted
colour, with the Trident tile to its left at the cap height. `SiteNav.tsx`
and `SiteFooter.tsx` are the reference rendering.

Write it "Psych Runtime" in prose, "Psych" once the context is clear. Never
"PsychRuntime", "psych runtime" or "PSYCH".

## Names

| Thing | Name |
|---|---|
| The product | Psych Runtime |
| The distribution on PyPI | `psych-runtime` |
| The import | `psych_runtime` |
| The command line | `psych` |
| The repository | `psych-systems/psych-runtime` |
| The site | psychruntime.com |
| The example application | the example console (`examples/playground/`) |
| The interactive page on the site | the browser demo (simulated) |

`pip install psych` installs an unrelated package. Every install instruction
says `psych-runtime`.

## Colours

The palette and the roles it plays are defined once in
`web/site/app/global.css` and read from there. The values:

| Role | Light | Dark |
|---|---|---|
| Paper (background) | `#F9F8F4` | `#0D1117` |
| Deeper paper (sunk sections) | `#F0EDE6` | `#161C24` |
| Ink (text) | `#151921` | `#E9ECF1` |
| Copper (accent) | `#BC4900` | `#F18B48` |
| Pale copper (accent surface) | `#F7DFCF` | copper at low alpha |

Copper is the one saturated colour. It marks the primary action, the current
record and the mark, and nothing else. Text on paper uses ink; text on copper
is white. Every pairing on the site is measured against WCAG AA in both themes
by `web/site/scripts/checks/contrast.mjs`; do not introduce a pairing that
check has not seen.

## Type

Fraunces, italic, for one phrase in a heading. IBM Plex Sans for everything
read. IBM Plex Mono for code, record types and field names. All three are
bundled with the site rather than fetched from a third party.

## Third parties

Psych Runtime is not affiliated with, endorsed by, or a product of any model
provider, database vendor or protocol body it integrates with. Their names
appear on the site to say what the library connects to, as plain text and
not as logos. If you write about Psych, say the same.
