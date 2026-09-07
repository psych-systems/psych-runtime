/**
 * The art inside the bento tiles.
 *
 * Each one draws something the runtime actually does, at the size a tile gives
 * it: the log with a gap where a Worker died, the four kinds of tool, a call
 * held for approval, tokens split by cache state, four stores behind one port.
 * Nothing here is stock decoration, and none of it invents a number the
 * repository cannot back.
 *
 * All of it is inline SVG on `currentColor` and the theme roles, so both
 * themes get the same drawing without a second asset.
 */

const FLAME_STOPS = (
  <>
    <stop offset="0%" stopColor="#ff8a45" />
    <stop offset="100%" stopColor="#bc4900" />
  </>
);

/** The record log as bars: a Worker dies mid-run, another picks it up. */
export function ArtRecovery() {
  // Heights are just a rhythm; the gap at index 7 is the point.
  const bars = [12, 20, 15, 26, 34, 22, 30, 0, 18, 28, 36, 24, 32, 44, 38];
  return (
    <svg viewBox="0 0 240 96" className="art" role="img" aria-label="A run's records as bars, with a gap where one Worker died and a second Worker continued">
      <defs>
        <linearGradient id="art-rec" x1="0" y1="1" x2="0" y2="0">
          {FLAME_STOPS}
        </linearGradient>
      </defs>
      {bars.map((h, i) =>
        h === 0 ? (
          <g key={i}>
            <rect x={i * 15 + 6} y={20} width={7} height={56} rx={3.5} fill="var(--line)" />
            <path d={`M${i * 15 + 3} 48 h13`} stroke="var(--accent)" strokeWidth="1.5" strokeDasharray="2 2.5" />
          </g>
        ) : (
          <rect
            key={i}
            x={i * 15 + 6}
            y={76 - h}
            width={7}
            height={h}
            rx={3.5}
            fill="url(#art-rec)"
            opacity={i < 7 ? 0.55 : 1}
          />
        ),
      )}
      <text x="6" y="92" fontSize="8.5" fontFamily="var(--font-mono)" fill="var(--text-faint)">
        attempt 1
      </text>
      <text x="128" y="92" fontSize="8.5" fontFamily="var(--font-mono)" fill="var(--accent-text)">
        attempt 2 · replayed
      </text>
    </svg>
  );
}

/** A destructive call, stopped for a person. */
export function ArtApproval() {
  return (
    <svg viewBox="0 0 240 96" className="art" role="img" aria-label="A call to issue_refund held for approval, with an approve and a deny control">
      <rect x="14" y="14" width="212" height="44" rx="12" fill="var(--card)" stroke="var(--line)" />
      <circle cx="36" cy="36" r="11" fill="var(--flame-fill, #bc4900)" opacity="0.14" />
      <path d="M32 36.5 l3 3 l6-7" stroke="var(--accent)" strokeWidth="2" fill="none" strokeLinecap="round" strokeLinejoin="round" />
      <text x="56" y="31" fontSize="11" fontFamily="var(--font-mono)" fill="var(--text)">
        issue_refund
      </text>
      <text x="56" y="46" fontSize="9.5" fontFamily="var(--font-mono)" fill="var(--text-faint)">
        cents=4200 · destructive
      </text>
      <rect x="14" y="66" width="72" height="24" rx="12" fill="var(--flame-fill, #bc4900)" />
      <text x="50" y="81.5" fontSize="10" fontFamily="var(--font-mono)" fill="#fff" textAnchor="middle">
        approve
      </text>
      <rect x="94" y="66" width="60" height="24" rx="12" fill="none" stroke="var(--line-strong)" />
      <text x="124" y="81.5" fontSize="10" fontFamily="var(--font-mono)" fill="var(--text-soft)" textAnchor="middle">
        deny
      </text>
      <text x="166" y="81.5" fontSize="9.5" fontFamily="var(--font-mono)" fill="var(--text-faint)">
        lease released
      </text>
    </svg>
  );
}

/** Tokens, split by cache state, which is why cost can be right. */
export function ArtCost() {
  return (
    <svg viewBox="0 0 240 96" className="art" role="img" aria-label="Token usage per model call, split into fresh input, cached reads and output">
      <defs>
        <linearGradient id="art-cost" x1="0" y1="0" x2="1" y2="0">
          {FLAME_STOPS}
        </linearGradient>
      </defs>
      {[
        { y: 14, fresh: 62, cache: 0, out: 22 },
        { y: 36, fresh: 18, cache: 96, out: 14 },
        { y: 58, fresh: 12, cache: 122, out: 18 },
      ].map((r, i) => (
        <g key={i}>
          <rect x="14" y={r.y} width={r.fresh} height={12} rx={6} fill="url(#art-cost)" />
          <rect x={14 + r.fresh + 3} y={r.y} width={r.cache} height={12} rx={6} fill="var(--violet)" opacity="0.55" />
          <rect x={14 + r.fresh + r.cache + 6} y={r.y} width={r.out} height={12} rx={6} fill="var(--green)" opacity="0.8" />
        </g>
      ))}
      <g fontSize="9" fontFamily="var(--font-mono)" fill="var(--text-faint)">
        <text x="14" y="88">
          input
        </text>
        <text x="58" y="88" fill="var(--violet-text)">
          cache_read
        </text>
        <text x="126" y="88" fill="var(--green-text)">
          output
        </text>
        <text x="186" y="88">
          cost=None
        </text>
      </g>
    </svg>
  );
}

