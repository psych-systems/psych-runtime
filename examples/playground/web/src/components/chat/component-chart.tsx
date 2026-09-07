"use client";

import { useId } from "react";

import type { ChartComponent, ChartPoint, ChartSeries } from "@/lib/types";

/**
 * An agent's chart, drawn here rather than shipped by the library.
 *
 * `psych` sends series, axis labels and a `mark` and stops there: no colour,
 * no geometry, no drawing commands (see `psych_runtime/core/components.py`). So every
 * decision below belongs to this console. The palette, the tick count and
 * the mark specs can all change without the agent, the log or the version
 * hash knowing. That is the whole point of the split: brand control costs nothing
 * because there was never any styling to override.
 *
 * ## Inline SVG, and no charting dependency
 *
 * Four marks, five series and no interaction beyond a tooltip is less code
 * than the adapter layer any charting library would need, and it inherits the
 * console's theme for free: every colour here is a CSS variable, so light and
 * dark are the same markup rather than two renders. A dependency would also
 * have to be told, in its own vocabulary, all the things this file says once.
 *
 * ## The palette is validated, not chosen
 *
 * `--series-1..5` are five hues whose every adjacent pair clears the
 * colour-vision and normal-vision separation floors on both surfaces
 * (`globals.css` records the measurement). Assigned in fixed order and never
 * cycled. A sixth series is refused by the library's own cap rather than
 * handed a repeated colour, because two series in one colour is a chart that
 * lies.
 *
 * Colour is never the only channel. Two or more series always get a legend,
 * the last point of each line is labelled directly, and every mark carries a
 * `<title>` so the value is reachable by pointer as well as by eye.
 */

const PALETTE = [
  "var(--series-1)",
  "var(--series-2)",
  "var(--series-3)",
  "var(--series-4)",
  "var(--series-5)",
] as const;

const WIDTH = 640;
const HEIGHT = 248;
// The right pad is wide enough to hold a line's end-label, which is where
// the direct labelling happens; without the room the label would either
// clip or have to move onto the plot and sit on top of the mark.
const PAD = { top: 16, right: 52, bottom: 40, left: 52 };
const PLOT = {
  width: WIDTH - PAD.left - PAD.right,
  height: HEIGHT - PAD.top - PAD.bottom,
};
/** Bars are capped rather than filling their slot: the leftover is the air
 *  that makes a group readable. */
const MAX_BAR = 24;
/** White doing the separating, in the surface colour, never a stroke around
 *  the mark. One width everywhere so neighbours read as neighbours. */
const GAP = 2;

export function ComponentChart({ chart }: { chart: ChartComponent }) {
  const series = chart.series.filter((item) => item.points.length > 0);
  if (series.length === 0) {
    return (
      <p className="text-caption text-muted-foreground">
        {chart.title || "Chart"}, no data was sent with it.
      </p>
    );
  }

  return (
    <figure className="flex w-full flex-col gap-2">
      {chart.title && <figcaption className="text-caption font-medium">{chart.title}</figcaption>}
      {chart.mark === "pie" ? (
        <PieChart chart={chart} series={series} />
      ) : (
        <CartesianChart chart={chart} series={series} />
      )}
      {series.length > 1 && <Legend names={series.map((item, i) => [item.name || `Series ${i + 1}`, i])} />}
    </figure>
  );
}

/** Line, area and bar share an axis frame, a scale and a tick strategy. */
function CartesianChart({ chart, series }: { chart: ChartComponent; series: ChartSeries[] }) {
  const clipId = useId();
  const categories = categoriesOf(series);
  const values = series.flatMap((item) => item.points.map((point) => point.y));
  const max = niceMax(Math.max(...values, 0));
  const min = Math.min(...values, 0);
  const ticks = [0, 0.5, 1].map((fraction) => min + (max - min) * fraction);

  const x = (index: number) =>
    categories.length === 1
      ? PAD.left + PLOT.width / 2
      : PAD.left + (index / (categories.length - 1)) * PLOT.width;
  const y = (value: number) =>
    PAD.top + PLOT.height - ((value - min) / (max - min || 1)) * PLOT.height;
  const band = PLOT.width / categories.length;
  const baseline = y(Math.max(min, 0));
  const bars =
    chart.mark === "bar"
      ? barPlacements(series, categories, band, y, baseline)
      : [];
  // A number on every bar is chaos past a dozen of them; under that it is the
  // relief the light palette needs, since three of its five steps sit below
  // 3:1 against the surface and colour alone must not carry the reading.
  const labelBars = bars.length <= 12 && bars.every((bar) => bar.width >= 16);

  return (
    <svg
      viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
      className="h-auto w-full"
      role="img"
      aria-label={chart.title || "Chart"}
    >
      <defs>
        <clipPath id={clipId}>
          <rect x={PAD.left} y={PAD.top - 4} width={PLOT.width} height={PLOT.height + 4} />
        </clipPath>
      </defs>

      {/* Recessive by design: hairline, solid, one step off the surface. */}
      {ticks.map((value) => (
        <g key={value}>
          <line
            x1={PAD.left}
            x2={WIDTH - PAD.right}
            y1={y(value)}
            y2={y(value)}
            stroke="var(--border)"
            strokeWidth={1}
          />
          <text
            x={PAD.left - 8}
            y={y(value) + 4}
            textAnchor="end"
            className="fill-muted-foreground tabular"
            fontSize={11}
          >
            {formatTick(value)}
          </text>
        </g>
      ))}

      <g clipPath={`url(#${clipId})`}>
        {chart.mark === "bar"
          ? bars.map((bar, index) => (
              <path
                key={index}
                d={roundedBar(bar.x, bar.y, bar.width, baseline - bar.y)}
                fill={PALETTE[bar.series % PALETTE.length]}
              >
                <title>{bar.title}</title>
              </path>
            ))
          : series.map((item, seriesIndex) => (
              <LineMark
                key={seriesIndex}
                item={item}
                colour={PALETTE[seriesIndex % PALETTE.length]}
                area={chart.mark === "area"}
                categories={categories}
                x={x}
                y={y}
                baseline={baseline}
              />
            ))}
      </g>

      {/* Outside the clip, because a bar at the top of the scale would
          otherwise have its own label cropped by the plot it fills. */}
      {labelBars &&
        bars.map((bar, index) => (
          <text
            key={index}
            x={bar.x + bar.width / 2}
            y={bar.y - 5}
            textAnchor="middle"
            className="fill-foreground tabular"
            fontSize={11}
          >
            {formatTick(bar.value)}
          </text>
        ))}

      {/* Labels selectively: the last reading of each line, never a number on
          every point. The axis and the tooltips carry the rest. */}
      {chart.mark !== "bar" &&
        series.map((item, seriesIndex) => {
          const last = item.points[item.points.length - 1];
          const index = categories.indexOf(last.x);
          if (index === -1) return null;
          return (
            <text
              key={seriesIndex}
              x={x(index) + 8}
              y={y(last.y) + 4}
              className="fill-foreground tabular"
              fontSize={11}
            >
              {formatTick(last.y)}
            </text>
          );
        })}

      {/* First and last only. A tick under every point collides the moment a
          series has more than a handful, and the tooltip has the rest. Bars
          are labelled under their band rather than at the plot edge, because a
          bar sits in the middle of its band and a label at the edge names the
          wrong thing. */}
      <text
        x={chart.mark === "bar" ? PAD.left + band / 2 : PAD.left}
        y={HEIGHT - 22}
        textAnchor={chart.mark === "bar" ? "middle" : "start"}
        className="fill-muted-foreground"
        fontSize={11}
      >
        {String(categories[0] ?? "")}
      </text>
      {categories.length > 1 && (
        <text
          x={
            chart.mark === "bar"
              ? PAD.left + band * (categories.length - 0.5)
              : WIDTH - PAD.right
          }
          y={HEIGHT - 22}
          textAnchor={chart.mark === "bar" ? "middle" : "end"}
          className="fill-muted-foreground"
          fontSize={11}
        >
          {String(categories[categories.length - 1])}
        </text>
      )}
      {chart.x_label && (
        <text
          x={PAD.left + PLOT.width / 2}
          y={HEIGHT - 6}
          textAnchor="middle"
          className="fill-muted-foreground"
          fontSize={11}
        >
          {chart.x_label}
        </text>
      )}
      {chart.y_label && (
        <text
          x={-(PAD.top + PLOT.height / 2)}
          y={12}
          transform="rotate(-90)"
          textAnchor="middle"
          className="fill-muted-foreground"
          fontSize={11}
        >
          {chart.y_label}
        </text>
      )}
    </svg>
  );
}

function LineMark({
  item,
  colour,
  area,
  categories,
  x,
  y,
  baseline,
}: {
  item: ChartSeries;
  colour: string;
  area: boolean;
  categories: (string | number)[];
  x: (index: number) => number;
  y: (value: number) => number;
  baseline: number;
}) {
  const placed = item.points
    .map((point) => ({ point, index: categories.indexOf(point.x) }))
    .filter((entry) => entry.index !== -1);
  if (placed.length === 0) return null;

  const path = placed
    .map((entry, i) => `${i === 0 ? "M" : "L"}${x(entry.index)},${y(entry.point.y)}`)
    .join(" ");
  const last = placed[placed.length - 1];

  return (
    <g>
      {area && (
        // A wash, never a saturated block: the line is the mark and the fill
        // only says which side of it the magnitude is on.
        <path
          d={`${path} L${x(last.index)},${baseline} L${x(placed[0].index)},${baseline} Z`}
          fill={colour}
          opacity={0.1}
        />
      )}
      <path d={path} fill="none" stroke={colour} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
      {placed.map((entry, i) => (
        // The dot is the hit target as well as the mark, and its surface ring
        // keeps it legible where two lines cross.
        <circle
          key={i}
          cx={x(entry.index)}
          cy={y(entry.point.y)}
          r={4}
          fill={colour}
          stroke="var(--card)"
          strokeWidth={GAP}
        >
          <title>{`${item.name ? `${item.name} · ` : ""}${entry.point.x}: ${formatTick(entry.point.y)}`}</title>
        </circle>
      ))}
    </g>
  );
}

interface BarPlacement {
  series: number;
  x: number;
  y: number;
  width: number;
  value: number;
  title: string;
}

/**
 * Where every bar goes, worked out once.
 *
 * Bars sit in a band each rather than on the point, so a two-bar chart does
 * not put half of each bar off the edge of the plot, and a group of series is
 * centred in its band with the leftover left as air. Computing them here
 * rather than while drawing is what lets the labels be drawn in a second pass
 * outside the clip path.
 */
function barPlacements(
  series: ChartSeries[],
  categories: (string | number)[],
  band: number,
  y: (value: number) => number,
  baseline: number
): BarPlacement[] {
  const width = Math.min(MAX_BAR, (band * 0.7) / series.length);
  const groupWidth = width * series.length + GAP * (series.length - 1);
  const placed: BarPlacement[] = [];
  series.forEach((item, seriesIndex) => {
    for (const point of item.points) {
      const at = categories.indexOf(point.x);
      if (at === -1) continue;
      const centre = PAD.left + band * (at + 0.5);
      const top = y(point.y);
      if (baseline - top <= 0) continue;
      placed.push({
        series: seriesIndex,
        x: centre - groupWidth / 2 + seriesIndex * (width + GAP),
        y: top,
        width,
        value: point.y,
        title: `${item.name ? `${item.name} · ` : ""}${point.x}: ${formatTick(point.y)}`,
      });
    }
  });
  return placed;
}

/**
 * A bar with a 4px rounded data-end and a square foot on the baseline.
 *
 * Rounding both ends would detach the bar from the axis it is measured
 * against; rounding neither makes a column of them read as a block.
 */
function roundedBar(x: number, y: number, width: number, height: number): string {
  const radius = Math.min(4, width / 2, Math.abs(height));
  if (height <= 0) return "";
  return [
    `M${x},${y + height}`,
    `L${x},${y + radius}`,
    `Q${x},${y} ${x + radius},${y}`,
    `L${x + width - radius},${y}`,
    `Q${x + width},${y} ${x + width},${y + radius}`,
    `L${x + width},${y + height}`,
    "Z",
  ].join(" ");
}

/**
 * A share of a whole.
 *
 * The weakest of the four marks and the library still offers it, because the
 * agent's `mark` is its reading of the data rather than an instruction and
 * "these are parts of one thing" is worth honouring. What makes it readable is
 * everything except the geometry: every slice is labelled with its share, the
 * legend names them all, and the surface gap keeps two adjacent slices from
 * merging into one.
 */
function PieChart({ chart, series }: { chart: ChartComponent; series: ChartSeries[] }) {
  const points = series[0].points.filter((point) => point.y > 0);
  const total = points.reduce((sum, point) => sum + point.y, 0);
  if (total === 0 || points.length === 0) {
    return <p className="text-caption text-muted-foreground">Nothing to show a share of.</p>;
  }

  const centre = { x: 130, y: 130 };
  const radius = 96;
  const slices = sliceArcs(points, total, centre, radius);

  return (
    <div className="flex flex-wrap items-center gap-4">
      <svg
        viewBox="0 0 260 260"
        className="h-auto w-[220px] shrink-0"
        role="img"
        aria-label={chart.title || "Share chart"}
      >
        {slices.map(({ point, path }, index) => (
          <path
            key={index}
            d={path}
            fill={PALETTE[index % PALETTE.length]}
            stroke="var(--card)"
            strokeWidth={GAP}
          >
            <title>{`${point.x}: ${formatTick(point.y)} (${share(point, total)})`}</title>
          </path>
        ))}
      </svg>
      {/* The legend is the labelling here rather than a supplement to it: a
          share written beside its own name is read; the same number crammed
          into a thin slice is not. */}
      <ul className="flex min-w-0 flex-col gap-1">
        {points.map((point, index) => (
          <li key={index} className="flex items-baseline gap-2 text-caption">
            <span
              aria-hidden
              className="mt-0.5 size-2.5 shrink-0 rounded-[2px]"
              style={{ backgroundColor: PALETTE[index % PALETTE.length] }}
            />
            <span className="min-w-0 truncate">{String(point.x)}</span>
            <span className="tabular ml-auto shrink-0 text-muted-foreground">
              {share(point, total)}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * Every slice's path, in one pass before the render rather than accumulated
 * during it: a running angle mutated inside a `map` is state hiding in the
 * render pass, and it reads correctly exactly once.
 */
function sliceArcs(
  points: ChartPoint[],
  total: number,
  centre: { x: number; y: number },
  radius: number
): { point: ChartPoint; path: string }[] {
  const made: { point: ChartPoint; path: string }[] = [];
  // Twelve o'clock, because that is where a reader starts looking.
  let from = -Math.PI / 2;
  for (const point of points) {
    const sweep = (point.y / total) * Math.PI * 2;
    made.push({ point, path: arc(centre, radius, from, from + sweep) });
    from += sweep;
  }
  return made;
}

function arc(
  centre: { x: number; y: number },
  radius: number,
  from: number,
  to: number
): string {
  // A single slice covering the whole circle has no arc to draw: its start and
  // end points coincide and the path collapses. Two halves instead.
  if (to - from >= Math.PI * 2 - 1e-6) {
    return [
      `M${centre.x},${centre.y - radius}`,
      `A${radius},${radius} 0 1 1 ${centre.x},${centre.y + radius}`,
      `A${radius},${radius} 0 1 1 ${centre.x},${centre.y - radius}`,
      "Z",
    ].join(" ");
  }
  const start = { x: centre.x + radius * Math.cos(from), y: centre.y + radius * Math.sin(from) };
  const end = { x: centre.x + radius * Math.cos(to), y: centre.y + radius * Math.sin(to) };
  const large = to - from > Math.PI ? 1 : 0;
  return [
    `M${centre.x},${centre.y}`,
    `L${start.x},${start.y}`,
    `A${radius},${radius} 0 ${large} 1 ${end.x},${end.y}`,
    "Z",
  ].join(" ");
}

/** Identity carried by a swatch beside the name, never by colouring the name:
 *  a light hue is illegible as text on either surface. */
function Legend({ names }: { names: [string, number][] }) {
  return (
    <ul className="flex flex-wrap items-center gap-x-4 gap-y-1">
      {names.map(([name, index]) => (
        <li key={index} className="flex items-center gap-1.5 text-caption text-muted-foreground">
          <span
            aria-hidden
            className="h-0.5 w-4 rounded-full"
            style={{ backgroundColor: PALETTE[index % PALETTE.length] }}
          />
          {name}
        </li>
      ))}
    </ul>
  );
}

/**
 * The x positions every series is plotted against.
 *
 * Taken from the longest series rather than the union, and in its order: an
 * agent sends readings in the order they were taken, and re-sorting them would
 * silently redraw a chart it did not send. A point whose x is not in this list
 * is dropped rather than appended, which keeps two series that disagree about
 * their x axis from stretching the plot.
 */
function categoriesOf(series: ChartSeries[]): (string | number)[] {
  const longest = series.reduce((best, item) =>
    item.points.length > best.points.length ? item : best
  );
  return longest.points.map((point) => point.x);
}

/** A round number at or above the data, so the top gridline reads cleanly. */
function niceMax(max: number): number {
  if (max <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(max));
  return Math.ceil(max / (magnitude / 2)) * (magnitude / 2);
}

const COMPACT = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 });
const PLAIN = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });

function formatTick(value: number): string {
  return Math.abs(value) >= 10_000 ? COMPACT.format(value) : PLAIN.format(value);
}

function share(point: ChartPoint, total: number): string {
  return `${Math.round((point.y / total) * 100)}%`;
}
