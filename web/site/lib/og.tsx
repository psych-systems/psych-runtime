import { readFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { ImageResponse } from "next/og";

/*
 * The social image and the touch icon are rendered at build time from the
 * same palette and the same Trident paths as the site.
 *
 * The two faces are read from `assets/fonts/` rather than fetched. The image
 * renderer needs a real font file, and a build that reaches out to a font CDN
 * fails in exactly the environments a static export exists to survive: an
 * offline machine, a CI runner behind a proxy, a network hiccup on deploy day.
 * Both files are SIL Open Font License, the same faces `next/font` self-hosts
 * for the pages themselves.
 */

const FONTS = resolve(process.cwd(), "assets", "fonts");

export const OG_SIZE = { width: 1200, height: 630 };

const PAPER = "#f9f8f4";
const INK = "#151921";
const COPPER = "#bc4900";
const COPPER_BRIGHT = "#f18b48";
const NIGHT = "#0d1117";
const NIGHT_TEXT_SOFT = "#a7afbb";

async function font(file: string) {
  const buffer = await readFile(join(FONTS, file));
  return new Uint8Array(buffer).buffer as ArrayBuffer;
}

const TRIDENT = [
  "M15.05 12.05L15.49 12.12L15.74 12.36L20.73 19.61L20.86 19.92L20.86 20.42L20.73 20.67L19.05 22.48L18.67 23.1L18.48 23.67L18.42 24.04L18.42 31.03L18.55 31.47L18.8 31.91L19.48 32.59L25.48 36.65L26.04 37.09L27.22 38.34L27.97 39.65L28.29 40.65L28.35 41.08L28.35 46.08L27.79 45.52L26.97 44.89L14.11 36.71L13.43 36.03L13.05 35.34L12.99 35.09L12.99 23.98L12.93 23.6L12.74 23.04L12.24 22.23L10.05 20.04L9.93 19.79L9.93 19.23L14.24 12.86L14.55 12.43L14.86 12.12Z",
  "M31.84 3L32.22 3L32.47 3.12L37.71 10.93L37.9 11.37L37.78 11.99L36.28 13.86L35.47 15.05L35.03 16.05L34.78 17.36L34.78 49.95L34.9 50.76L35.4 52.07L36.65 54.07L36.71 54.32L36.71 57.19L36.53 57.69L36.15 58.07L32.47 60.88L32.16 61L31.84 61L31.41 60.75L27.72 57.94L27.47 57.63L27.35 57.25L27.35 54.19L27.54 53.76L28.04 53.07L28.78 51.76L29.1 50.89L29.28 49.89L29.28 17.36L29.1 16.3L28.53 14.99L26.66 12.43L26.35 12.12L26.16 11.68L26.16 11.37L26.35 10.93L31.53 3.19Z",
  "M48.7 12.05L49.01 12.05L49.33 12.24L54.07 19.23L54.07 19.79L53.95 20.04L51.82 22.17L51.32 22.92L51.07 23.67L51.01 24.1L51.01 35.15L50.89 35.53L50.51 36.15L49.89 36.71L37.15 44.83L36.53 45.27L35.65 46.08L35.65 41.21L35.71 40.77L36.21 39.27L36.84 38.27L37.9 37.15L38.52 36.65L44.52 32.59L45.2 31.91L45.45 31.47L45.58 31.03L45.58 23.98L45.52 23.67L45.33 23.1L44.95 22.48L43.21 20.54L43.14 19.98L43.58 19.17L48.01 12.74L48.33 12.3Z",
];

function TridentPaths({ size, fill }: { size: number; fill: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 64 64" fill={fill}>
      {TRIDENT.map((d) => (
        <path key={d} d={d} />
      ))}
    </svg>
  );
}

export async function renderSocialImage({ title, kicker }: { title: string; kicker: string }) {
  const [fraunces, mono] = await Promise.all([font("fraunces-500.woff"), font("plex-mono-400.woff")]);
  return new ImageResponse(
    (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          background: PAPER,
          color: INK,
          fontFamily: "Fraunces",
        }}
      >
        <div style={{ display: "flex", flexDirection: "column", justifyContent: "space-between", padding: "64px 72px", width: 760 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 16, fontFamily: "IBM Plex Mono", fontSize: 22, color: COPPER }}>
            <div style={{ width: 28, height: 3, background: COPPER }} />
            {kicker}
          </div>
          <div style={{ display: "flex", fontSize: 64, lineHeight: 1.04, letterSpacing: -2, fontWeight: 500 }}>{title}</div>
          <div style={{ display: "flex", flexDirection: "column", gap: 6, fontFamily: "IBM Plex Mono", fontSize: 22, color: "#4f5662" }}>
            <div>pip install psych-runtime</div>
            <div style={{ color: "#7d838c" }}>psychruntime.com · Apache-2.0 · Python 3.12+</div>
          </div>
        </div>
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            justifyContent: "space-between",
            width: 440,
            background: NIGHT,
            borderLeft: `3px solid ${COPPER}`,
            padding: "56px 48px",
            color: NIGHT_TEXT_SOFT,
            fontFamily: "IBM Plex Mono",
            fontSize: 18,
          }}
        >
          <TridentPaths size={190} fill={COPPER_BRIGHT} />
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            <div style={{ display: "flex", gap: 14 }}>
              <span style={{ color: "#6e7683" }}>01</span>
              <span style={{ color: "#e9ecf1" }}>run_admitted</span>
            </div>
            <div style={{ display: "flex", gap: 14 }}>
              <span style={{ color: "#6e7683" }}>02</span>
              <span style={{ color: "#e9ecf1" }}>attempt_started</span>
            </div>
            <div style={{ display: "flex", gap: 14 }}>
              <span style={{ color: "#6e7683" }}>03</span>
              <span style={{ color: "#ad8bf2" }}>model_call_finished</span>
            </div>
            <div style={{ display: "flex", gap: 14 }}>
              <span style={{ color: "#6e7683" }}>04</span>
              <span style={{ color: COPPER_BRIGHT }}>tool_call_finished</span>
            </div>
            <div style={{ display: "flex", gap: 14 }}>
              <span style={{ color: "#6e7683" }}>05</span>
              <span style={{ color: "#65b98c" }}>run_settled</span>
            </div>
          </div>
        </div>
      </div>
    ),
    {
      ...OG_SIZE,
      fonts: [
        { name: "Fraunces", data: fraunces, weight: 500, style: "normal" },
        { name: "IBM Plex Mono", data: mono, weight: 400, style: "normal" },
      ],
    },
  );
}

export function renderAppleIcon() {
  return new ImageResponse(
    (
      <div style={{ width: "100%", height: "100%", display: "flex", alignItems: "center", justifyContent: "center", background: COPPER }}>
        <TridentPaths size={112} fill={PAPER} />
      </div>
    ),
    { width: 180, height: 180 },
  );
}
