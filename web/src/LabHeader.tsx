// Page header: a dead cassette on the slab. The reel hubs glow like eyes, the
// tape is torn and stitched back together, and the reels turn while the
// server is decoding. Decorative SVG only (aria-hidden); the heading and the
// intro text carry the meaning. Inline SVG needs no network request, so the
// CSP `default-src 'self'` is unaffected.
import "./theme.css";

export interface LabHeaderProps {
  /** True while a recovery is running; the reels spin. */
  busy: boolean;
}

/** Six-tooth cassette hub at (cx, cy). */
function Reel({ cx, cy, wound }: { cx: number; cy: number; wound: number }) {
  const teeth = [0, 60, 120, 180, 240, 300];
  return (
    <g>
      <circle cx={cx} cy={cy} r={wound} className="cassette-wound" />
      <g className="cassette-reel">
        <circle cx={cx} cy={cy} r={8.5} className="cassette-hub" />
        {teeth.map((a) => (
          <rect
            key={a}
            x={cx - 1.2}
            y={cy - 8.5}
            width={2.4}
            height={3.2}
            className="cassette-tooth"
            transform={`rotate(${a} ${cx} ${cy})`}
          />
        ))}
        <circle cx={cx} cy={cy} r={3.4} className="cassette-eye" />
      </g>
    </g>
  );
}

/** Cross-stitches over a tear in the tape, centred on x. */
function Stitches({ x, y }: { x: number; y: number }) {
  return (
    <g className="cassette-stitches">
      {[-9, -3, 3, 9].map((dx) => (
        <g key={dx}>
          <line x1={x + dx - 2.5} y1={y - 5} x2={x + dx + 2.5} y2={y + 5} />
          <line x1={x + dx + 2.5} y1={y - 5} x2={x + dx - 2.5} y2={y + 5} />
        </g>
      ))}
    </g>
  );
}

export function Cassette() {
  return (
    <svg className="cassette" viewBox="0 0 260 168" aria-hidden="true" focusable="false">
      {/* shell and screws */}
      <rect x="4" y="4" width="252" height="160" rx="12" className="cassette-shell" />
      {[
        [16, 16],
        [244, 16],
        [16, 152],
        [244, 152],
      ].map(([x, y]) => (
        <circle key={`${x}-${y}`} cx={x} cy={y} r="3.2" className="cassette-screw" />
      ))}
      {/* label */}
      <rect x="22" y="14" width="216" height="100" rx="6" className="cassette-label" />
      <rect x="22" y="24" width="216" height="5" className="cassette-label-stripe" />
      <text x="34" y="47" className="cassette-label-title">
        STARMAP
      </text>
      <text x="226" y="47" textAnchor="end" className="cassette-label-small">
        SIDE A
      </text>
      <text x="34" y="106" className="cassette-label-scrawl">
        do not erase
      </text>
      <text x="226" y="106" textAnchor="end" className="cassette-label-scrawl">
        block 6 †
      </text>
      {/* window with two reels */}
      <rect x="60" y="56" width="140" height="40" rx="7" className="cassette-window" />
      <Reel cx={96} cy={76} wound={17} />
      <Reel cx={164} cy={76} wound={11} />
      {/* ectoplasm dripping off the bottom of the label */}
      {(
        [
          [58, 8],
          [104, 13],
          [111, 6],
          [190, 10],
        ] as const
      ).map(([x, len]) => (
        <path
          key={x}
          className="cassette-drip"
          d={`M${x} 112 h5 v${len} a2.5 2.5 0 0 1 -5 0 z`}
          style={{ animationDelay: `${(x % 7) * 0.4}s` }}
        />
      ))}
      {/* exposed tape at the head opening, torn and stitched */}
      <path d="M44 134 H126" className="cassette-tape" />
      <path d="M134 134 H216" className="cassette-tape" />
      <Stitches x={130} y={134} />
      {/* lower guide */}
      <path d="M52 164 L66 142 H194 L208 164" className="cassette-guide" />
      <circle cx="86" cy="153" r="5" className="cassette-hole" />
      <circle cx="174" cy="153" r="5" className="cassette-hole" />
    </svg>
  );
}

export function LabHeader({ busy }: LabHeaderProps) {
  return (
    <header className={busy ? "lab lab--busy" : "lab"}>
      <Cassette />
      <div className="lab-text">
        <h1 className="lab-title">TapeRescue</h1>
        <p className="lab-tagline">Two dead recordings. One program, stitched back to life.</p>
        <p className="lab-intro">
          Recover BBC Micro cassette programs from one or two WAV captures (44,100 Hz, 16-bit, mono PCM).
        </p>
      </div>
    </header>
  );
}

/** A heartbeat trace drawn while the decoder runs. Decorative. */
export function Heartbeat() {
  return (
    <svg className="heartbeat" viewBox="0 0 120 24" aria-hidden="true" focusable="false">
      <path d="M0 12 H34 L40 3 L46 21 L52 7 L56 12 H120" />
    </svg>
  );
}
