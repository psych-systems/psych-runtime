import type { SVGProps } from "react";

/**
 * The Psych mark: psi drawn as a trident.
 *
 * Three bladed prongs on a shaft, each tapering to a point, with the outer two
 * hooking in toward the base. Filled paths rather than strokes, so the weight
 * varies along a blade and the mark still reads at 16px, which a uniform stroke
 * does not.
 *
 * The three prongs are the three ports a consumer supplies. The shaft they
 * stand on is the record log. `TridentDiagram` draws that reading with labels; this
 * is the plain mark.
 */
export const TRIDENT_PATHS = [
  // Left prong, centre shaft and right prong. Three separate shapes, which is
  // how the drawing is built: the prongs stop short of the shaft rather than
  // merging into it, and that gap is what gives the mark its air.
  //
  // Traced from the source artwork by contour extraction rather than redrawn by
  // eye. Roughly one per cent of the mark's pixels differ from the original,
  // all of it edge antialiasing. Do not hand-edit these; retrace if the artwork
  // changes.
  "M15.05 12.05L15.49 12.12L15.74 12.36L20.73 19.61L20.86 19.92L20.86 20.42L20.73 20.67L19.05 22.48L18.67 23.1L18.48 23.67L18.42 24.04L18.42 31.03L18.55 31.47L18.8 31.91L19.48 32.59L25.48 36.65L26.04 37.09L27.22 38.34L27.97 39.65L28.29 40.65L28.35 41.08L28.35 46.08L27.79 45.52L26.97 44.89L14.11 36.71L13.43 36.03L13.05 35.34L12.99 35.09L12.99 23.98L12.93 23.6L12.74 23.04L12.24 22.23L10.05 20.04L9.93 19.79L9.93 19.23L14.24 12.86L14.55 12.43L14.86 12.12Z",
  "M31.84 3L32.22 3L32.47 3.12L37.71 10.93L37.9 11.37L37.78 11.99L36.28 13.86L35.47 15.05L35.03 16.05L34.78 17.36L34.78 49.95L34.9 50.76L35.4 52.07L36.65 54.07L36.71 54.32L36.71 57.19L36.53 57.69L36.15 58.07L32.47 60.88L32.16 61L31.84 61L31.41 60.75L27.72 57.94L27.47 57.63L27.35 57.25L27.35 54.19L27.54 53.76L28.04 53.07L28.78 51.76L29.1 50.89L29.28 49.89L29.28 17.36L29.1 16.3L28.53 14.99L26.66 12.43L26.35 12.12L26.16 11.68L26.16 11.37L26.35 10.93L31.53 3.19Z",
  "M48.7 12.05L49.01 12.05L49.33 12.24L54.07 19.23L54.07 19.79L53.95 20.04L51.82 22.17L51.32 22.92L51.07 23.67L51.01 24.1L51.01 35.15L50.89 35.53L50.51 36.15L49.89 36.71L37.15 44.83L36.53 45.27L35.65 46.08L35.65 41.21L35.71 40.77L36.21 39.27L36.84 38.27L37.9 37.15L38.52 36.65L44.52 32.59L45.2 31.91L45.45 31.47L45.58 31.03L45.58 23.98L45.52 23.67L45.33 23.1L44.95 22.48L43.21 20.54L43.14 19.98L43.58 19.17L48.01 12.74L48.33 12.3Z",
] as const;

export function Trident({
  size = 24,
  title,
  ...rest
}: { size?: number; title?: string } & Omit<SVGProps<SVGSVGElement>, "width" | "height">) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      fill="currentColor"
      role={title ? "img" : undefined}
      aria-hidden={title ? undefined : true}
      {...rest}
    >
      {title ? <title>{title}</title> : null}
      {TRIDENT_PATHS.map((d) => (
        <path key={d} d={d} />
      ))}
    </svg>
  );
}

/** The mark on a copper tile, for the nav and anywhere it needs to hold a corner. */
export function TridentTile({ size = 34, className }: { size?: number; className?: string }) {
  return (
    <span className={className ? `trident-tile ${className}` : "trident-tile"} style={{ width: size, height: size }}>
      <Trident size={Math.round(size * 0.62)} />
    </span>
  );
}

/**
 * The mark read as a diagram: three prongs labelled with the ports a consumer
 * supplies, meeting in the stem that carries the log back out.
 *
 * Used once, where that reading is the argument rather than decoration.
 */
export function TridentDiagram({ className }: { className?: string }) {
  // The mark is drawn at translate(180 60) scale(2.5). In its own 64 box the
  // prong tips sit at (15.1, 12.1), (31.8, 3) and (48.7, 12.1), and the shaft
  // ends at y 61, so on this canvas that is x 205 and 316 at y 90, the centre
  // tip at y 68, and the foot at y 213. Every leader below is measured off
  // those numbers; retracing the mark means recomputing them.
  return (
    <svg
      viewBox="0 0 520 300"
      className={className}
      role="img"
      aria-labelledby="td-t td-d"
      fontFamily="var(--font-mono)"
    >
      <title id="td-t">Three ports meet in one record log</title>
      <desc id="td-d">
        The Psych mark drawn as a diagram. Its three prongs are labelled tools, model and store, the
        ports a consumer supplies. They stand on the shaft, labelled the record log, and everything
        read back out of a Run is a fold over that log.
      </desc>

      {/* leaders. fill none, or SVG paints the path black */}
      <g stroke="var(--diagram-line)" strokeWidth="1" strokeDasharray="3 4" fill="none">
        <path d="M259.5 52 V64" />
        <path d="M204 90 H150" />
        <path d="M316 90 H370" />
        <path d="M259.5 216 V232" />
      </g>

      <g transform="translate(180 60) scale(2.5)" fill="var(--diagram-mark)">
        {TRIDENT_PATHS.map((d) => (
          <path key={d} d={d} />
        ))}
      </g>

      <g fill="var(--diagram-label)" fontSize="13">
        <text x="259.5" y="30" textAnchor="middle">
          tools
        </text>
        <text x="142" y="94" textAnchor="end">
          model
        </text>
        <text x="378" y="94">
          store
        </text>
        <text x="259.5" y="254" textAnchor="middle" fill="var(--diagram-accent)">
          the record log
        </text>
      </g>
      <g fill="var(--diagram-muted)" fontSize="11">
        <text x="259.5" y="46" textAnchor="middle">
          code · http · mcp · a2a
        </text>
        <text x="142" y="110" textAnchor="end">
          ModelClient
        </text>
        <text x="378" y="110">
          Store
        </text>
        <text x="259.5" y="272" textAnchor="middle">
          report · status · answer · stream
        </text>
      </g>
    </svg>
  );
}
