/**
 * The atmosphere: grain, blooms and the curtain gradient.
 *
 * Three pieces, all pure CSS on absolutely positioned elements, all
 * `aria-hidden`, none of them in the flow.
 *
 * - `Grain` is one tiling fractal-noise SVG as a data URI. It is what stops a
 *   smooth gradient looking like a smooth gradient, and it is why the surfaces
 *   read as printed rather than rendered.
 * - `Bloom` is a soft radial wash. Large radial gradients are cheap; a blurred
 *   element is not, because `filter: blur()` on something the size of the
 *   viewport costs a full-surface repaint on every frame it moves. Nothing here
 *   uses a blur filter.
 * - `Curtain` is the vertical banding: columns of light and shade over a
 *   gradient, the way a plume of colour falls in the reference sheets.
 *
 * `Stage`, which clips them and reacts to the cursor, lives in its own file
 * because it is the only part of this that needs to run in the browser.
 *
 * Colour comes from theme roles, so the same three components paint a warm
 * near-white page and a deep near-black one without a second set of assets.
 */

/** Where a bloom sits and how big it is, as a percentage of its container. */
type BloomProps = {
  x: number;
  y: number;
  /** Width as a percentage of the container. Height follows `ratio`. */
  size: number;
  ratio?: number;
  /** 0 to 1. The tokens already sit low; this is the final trim. */
  opacity?: number;
  tone?: "flame" | "ember" | "gold";
  /** Drift slowly. Off by default: a page of moving backgrounds is a headache. */
  drift?: boolean;
  className?: string;
};

export function Bloom({
  x,
  y,
  size,
  ratio = 1,
  opacity = 1,
  tone = "flame",
  drift = false,
  className,
}: BloomProps) {
  return (
    <div
      aria-hidden
      className={`bloom bloom-${tone}${drift ? " bloom-drift" : ""}${className ? ` ${className}` : ""}`}
      style={{
        left: `${x}%`,
        top: `${y}%`,
        width: `${size}%`,
        aspectRatio: `${ratio}`,
        opacity,
      }}
    />
  );
}

/** The full-surface grain. One per stage, on top of everything it textures. */
export function Grain({ opacity }: { opacity?: number }) {
  return <div aria-hidden className="grain" style={opacity === undefined ? undefined : { opacity }} />;
}

/**
 * The curtain: a colour plume banded into vertical columns.
 *
 * `from` and `to` are percentages down the stage where the colour starts and
 * finishes, so a plume can rise from the floor or hang from the ceiling.
 */
export function Curtain({
  className,
  fade = "up",
}: {
  className?: string;
  fade?: "up" | "down";
}) {
  return <div aria-hidden className={`curtain curtain-${fade}${className ? ` ${className}` : ""}`} />;
}
