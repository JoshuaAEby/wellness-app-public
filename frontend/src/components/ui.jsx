/**
 * Card anatomy — the shared vocabulary every redesigned card is built from.
 *
 * Replaces 66 hand-repeated copies of `rounded-xl bg-slate-800 p-4` and 97 of
 * `text-xs font-medium uppercase tracking-widest text-slate-400`. The point isn't
 * brevity: it's that card chrome now has ONE definition, so changing the card
 * surface or the eyebrow treatment is a single edit rather than a 40-file sed.
 *
 * Structure, in order, matching the design mockups:
 *
 *   <Card>
 *     <Eyebrow left="RECOVERY · COMPOSITE" right="06:41" />
 *     <Hero value="50" unit={["OF 100","MODERATE"]} />
 *     <HeroSub>one sentence naming what the number means</HeroSub>
 *     …the figure…
 *     <Legend items={[…]} />
 *     <Foot>how to read the figure</Foot>
 *   </Card>
 *
 * Not every card needs every part. The rule is ordering, not completeness:
 * eyebrow says what this is, hero says the answer, hero-sub says why, the figure
 * shows the evidence, the foot explains the encoding.
 */

// Card surface. `tone="flat"` drops the border for cards nested inside another.
export function Card({ children, className = "", tone = "raised", ...rest }) {
  const border = tone === "flat" ? "" : "border border-rule";
  return (
    <div className={`rounded-2xl bg-card ${border} p-4 ${className}`} {...rest}>
      {children}
    </div>
  );
}

// Small caps label at the top of a card. `right` is the optional context slot
// (a timestamp, a count, a window) — it is never the headline.
export function Eyebrow({ left, right, className = "" }) {
  return (
    <div className={`flex items-baseline justify-between text-[9.5px] font-semibold tracking-eyebrow text-ink-3 ${className}`}>
      <span>{left}</span>
      {right != null && <span>{right}</span>}
    </div>
  );
}

// The single number the card exists to deliver. `unit` may be a string or an
// array of lines rendered beside it. `color` overrides the default ink — pass a
// state token (var(--over)/var(--under)) only when the value itself is a judgement.
export function Hero({ value, unit, color, className = "" }) {
  const lines = Array.isArray(unit) ? unit : unit != null ? [unit] : [];
  return (
    <div className={`mt-3 flex items-baseline gap-2.5 ${className}`}>
      <span className="text-[46px] font-semibold leading-[.9] tracking-[-.03em]"
        style={color ? { color } : undefined}>
        {value}
      </span>
      {lines.length > 0 && (
        <span className="text-[11px] leading-[1.35] tracking-[.1em] text-ink-3">
          {lines.map((l, i) => <span key={i} className="block">{l}</span>)}
        </span>
      )}
    </div>
  );
}

// One sentence under the hero, in the prose face. This is where the card earns
// its place — it should say what the number *means*, not restate it.
export function HeroSub({ children, className = "" }) {
  return (
    <p className={`prose-ui mt-2.5 text-[11.5px] leading-[1.6] text-ink-2 ${className}`}>
      {children}
    </p>
  );
}

// Emphasis inside a HeroSub. Kept as a component so the treatment stays uniform.
export function K({ children }) {
  return <b className="font-semibold text-ink">{children}</b>;
}

// Dot separator between HeroSub clauses.
export function Sep() {
  return <span className="mx-[7px] text-ink-3">·</span>;
}

// How to read the figure. Rule above it, prose face, recessive.
export function Foot({ children, className = "" }) {
  return (
    <p className={`prose-ui mt-3 border-t border-rule pt-3 text-[10.5px] leading-[1.6] text-ink-3 ${className}`}>
      {children}
    </p>
  );
}

// Legend. `items` = [{ label, color, hatch?, note? }]. Identity is always carried
// by a swatch beside a text label — never by colour alone.
export function Legend({ items, className = "" }) {
  return (
    <div className={`mt-2.5 flex flex-wrap gap-x-3 gap-y-1 text-[10.5px] text-ink-2 ${className}`}>
      {items.filter(Boolean).map((it) => (
        <span key={it.label} className="flex items-center gap-1.5">
          <i className="h-[7px] w-[7px] flex-none rounded-[1px]"
            style={it.hatch
              ? { background: `repeating-linear-gradient(45deg, ${it.color} 0 1.4px, var(--surface) 1.4px 4.4px)` }
              : it.outline
                ? { border: `1px solid ${it.color}` }
                : { background: it.color }} />
          {it.label}
          {it.note && <span className="text-ink-3">{it.note}</span>}
        </span>
      ))}
    </div>
  );
}

/**
 * A bar whose label and value sit INSIDE the track.
 *
 * The label-above-bar pattern this replaces cost two lines and a gap per metric —
 * four metrics filled a card with mostly type. Here the track IS the row: the fill
 * runs behind the text, so height drops by ~40% and the number sits where the
 * quantity is, not above it.
 *
 * Text stays in ink at full opacity while the fill is drawn beneath it, which keeps
 * it legible over both the filled and unfilled parts without needing a
 * knockout/blend trick that breaks in one of the two states.
 *
 *   value/goal   → fill fraction (clamped; overflow is shown by `over`, not width)
 *   tick         → optional reference marker in the same units (a goal, a mean)
 */
export function TrackBar({
  label, value, goal, unit = "", color, tick, tickLabel,
  mean = null, displayValue, sub, height = 24,
}) {
  const has = value != null;
  const pct = has && goal ? Math.min(100, (value / goal) * 100) : 0;
  const met = has && goal ? value >= goal : false;
  const tickPct = tick != null && goal ? Math.min(100, Math.max(0, (tick / goal) * 100)) : null;
  const fmt = (n) => (typeof n === "number" ? n.toLocaleString() : n);
  const at = (v) => Math.min(100, Math.max(0, (v / goal) * 100));
  const meanPct = mean?.value != null && goal ? at(mean.value) : null;
  const bandLo = mean?.lo != null && goal ? at(mean.lo) : null;
  const bandHi = mean?.hi != null && goal ? at(mean.hi) : null;
  return (
    <div>
      <div className="relative overflow-hidden rounded-md bg-surface" style={{ height }}>
        {/* Fill opacity is capped at 0.6: the label and value sit ON this, and a
            saturated fill puts mid-tone under near-white text at 10px, where it
            stops being reliably legible. "Goal met" is therefore signalled by a
            check rather than by driving the fill darker. */}
        <div className="absolute inset-y-0 left-0 transition-[width] duration-300"
          style={{ width: `${pct}%`, backgroundColor: color, opacity: met ? 0.6 : 0.45 }} />
        {/* Typical range (p25–p75) behind everything: the mean only becomes
            readable next to the spread it came from. Drawn as a lightening of the
            track rather than a colour, so it never competes with the fill. */}
        {bandLo != null && bandHi != null && bandHi > bandLo && (
          <div className="absolute inset-y-0 bg-ink/[0.13]"
            style={{ left: `${bandLo}%`, width: `${bandHi - bandLo}%` }}
            title={`typical ${fmt(mean.lo)}–${fmt(mean.hi)}${unit}`} />
        )}
        {meanPct != null && (
          <div className="absolute inset-y-0 w-px"
            style={{
              left: `${meanPct}%`,
              // A 1px element with `border-dashed` renders as a solid hairline in
              // WebKit, so the dashes come from a repeating gradient instead.
              backgroundImage:
                "repeating-linear-gradient(to bottom, var(--ink) 0 3px, transparent 3px 6px)",
              opacity: 0.9,
            }}
            title={`${mean.n}-day mean ${fmt(mean.value)}${unit}`} />
        )}
        {tickPct != null && (
          <div className="absolute inset-y-0 w-px opacity-60"
            style={{ left: `${tickPct}%`, backgroundColor: "var(--ink-2)" }} title={tickLabel} />
        )}
        <div className="relative flex h-full items-center justify-between gap-1 px-2">
          <span className="truncate text-[10px] font-semibold tracking-micro text-ink">{label}</span>
          <span className="flex-none text-[11px] font-semibold text-ink">
            {/* Goal met needs its own mark. Width alone can't say it: a bar is
                clamped at 100%, so 38/38 and 61/38 are the same rectangle. And it
                can't be a colour, because whether exceeding a goal is good depends
                on the metric — a protein floor and a calorie ceiling both live in
                this component. */}
            {met && <span className="mr-0.5" style={{ color }}>✓</span>}
            {has ? (displayValue ?? fmt(value)) : "—"}
            {goal != null && (
              // ink at 65% rather than ink-2: the mid grey vanishes against a
              // mid-tone fill, while dimmed white holds on both track and fill.
              <span className="font-normal text-ink/65"> / {fmt(goal)}{unit}</span>
            )}
          </span>
        </div>
      </div>
      {sub && <p className="mt-0.5 px-0.5 text-[9.5px] leading-none text-ink-3">{sub}</p>}
    </div>
  );
}

/**
 * A stacked proportion bar with each share labelled inside its own segment.
 *
 * Replaces bar + N legend rows (one line each) with a single row. Segments narrower
 * than `minLabel` percent stay unlabelled rather than clipping — the value is still
 * in the tooltip/sub line, and a truncated "3…" is worse than nothing.
 *
 * `segments` = [{ key, label, pct, color, title? }]
 */
export function StackBar({ segments, height = 22, minLabel = 13, className = "" }) {
  return (
    <div className={`flex gap-[2px] overflow-hidden rounded-md ${className}`} style={{ height }}>
      {segments.filter((s) => s.pct > 0).map((s) => (
        <div key={s.key} className="flex items-center justify-center overflow-hidden"
          style={{ width: `${s.pct}%`, backgroundColor: s.color }} title={s.title}>
          {s.pct >= minLabel && (
            <span className="truncate px-1 text-[9.5px] font-semibold leading-none text-canvas">
              {s.label}
            </span>
          )}
        </div>
      ))}
    </div>
  );
}

/**
 * Histogram — the shape of a distribution, not just its centre.
 *
 * Exists because summary lines hide structure that changes what you'd do. Sleep
 * duration reads "mean 7h15, median 7h29" — nearly identical numbers that conceal a
 * pronounced LEFT TAIL: a solid mode around 8h with a straggle of 3–6h nights
 * dragging the mean under the median. "Improve your average" and "eliminate the bad
 * nights" are different instructions, and only the shape distinguishes them.
 *
 *   values    raw observations
 *   binSize   bin width in the values' own units
 *   colorFor  (binStart) => colour, so a threshold can recolour part of the range
 *   refs      [{ value, label, dash }] reference lines drawn over the bars
 *   fmtX      (binStart) => axis label; ticks are placed every `tickEvery` bins
 */
export function Histogram({
  values, binSize = 30, colorFor, refs = [], fmtX = (v) => v,
  tickEvery = 2, height = 96, className = "",
}) {
  const v = (values ?? []).filter((x) => x != null);
  if (v.length < 3) return null;
  const lo = Math.floor(Math.min(...v) / binSize) * binSize;
  const hi = Math.ceil((Math.max(...v) + 1) / binSize) * binSize;
  const nBins = Math.max(1, Math.round((hi - lo) / binSize));
  const counts = new Array(nBins).fill(0);
  v.forEach((x) => {
    counts[Math.min(nBins - 1, Math.floor((x - lo) / binSize))] += 1;
  });
  const maxN = Math.max(...counts);

  const W = 320, X0 = 6, X1 = W - 6, PLOT = height - 16;
  const bw = (X1 - X0) / nBins;
  const px = (val) => X0 + ((val - lo) / (hi - lo)) * (X1 - X0);

  return (
    <svg viewBox={`0 0 ${W} ${height}`} className={`block w-full ${className}`} style={{ height }}
      role="img" aria-label="Distribution histogram">
      {counts.map((n, i) => {
        const h = maxN ? (n / maxN) * PLOT : 0;
        const binStart = lo + i * binSize;
        return (
          <g key={i}>
            <rect x={px(binStart) + 0.5} y={PLOT - h} width={Math.max(bw - 1, 1)} height={h}
              rx="1.5" fill={colorFor ? colorFor(binStart) : "var(--core)"} opacity="0.85" />
            {n > 0 && h > 12 && (
              <text x={px(binStart) + bw / 2} y={PLOT - h + 9} textAnchor="middle"
                fontSize="7.5" fill="var(--canvas)" fontWeight="600">{n}</text>
            )}
          </g>
        );
      })}
      <line x1={X0} y1={PLOT} x2={X1} y2={PLOT} stroke="var(--rule)" />
      {/* Reference labels stagger vertically when their lines are close together —
          a goal and a median can land a minute apart, and overprinted labels are
          worse than no labels. */}
      {(() => {
        const vis = refs.filter((r) => r.value >= lo && r.value <= hi)
          .sort((a, b) => a.value - b.value);
        let lastX = -Infinity, row = 0;
        return vis.map((r) => {
          const x = px(r.value);
          row = (x - lastX < 46) ? row + 1 : 0;
          lastX = x;
          const y = 8 + row * 9;
          const flip = x > W * 0.7;
          return (
            <g key={r.label}>
              <line x1={x} y1={0} x2={x} y2={PLOT}
                stroke="var(--ink)" strokeWidth="1.2" strokeDasharray={r.dash ?? "4 3"} opacity="0.85" />
              <text x={x} y={y} textAnchor={flip ? "end" : "start"} fontSize="7.5"
                fill="var(--ink-2)" dx={flip ? -3 : 3}>{r.label}</text>
            </g>
          );
        });
      })()}
      {counts.map((_, i) => i % tickEvery === 0 && (
        <text key={i} x={px(lo + i * binSize)} y={height - 3} textAnchor="middle"
          fontSize="7.5" fill="var(--ink-3)">{fmtX(lo + i * binSize)}</text>
      ))}
    </svg>
  );
}

// Segmented control for splitting one nav tab into views. Underline rather than a
// pill: it reads as "same place, different lens", where a pill reads as navigation.
export function SectionHeader({ label }) {
  return (
    <p className="pt-2 text-xs font-semibold uppercase tracking-widest text-ink-3 px-1">
      {label}
    </p>
  );
}

export function SegmentedTabs({ value, onChange, options, className = "" }) {
  return (
    <div className={`flex gap-6 border-b border-rule ${className}`} role="tablist">
      {options.map((o) => {
        const active = o.value === value;
        return (
          <button key={o.value} role="tab" aria-selected={active}
            onClick={() => onChange(o.value)}
            className={`-mb-px border-b-2 pb-2.5 text-[10px] font-semibold tracking-eyebrow transition-colors ${
              active ? "border-ink text-ink" : "border-transparent text-ink-3 hover:text-ink-2"
            }`}>
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

// A recessed well inside a card — for content that is related but shouldn't read
// as part of the main figure (e.g. a composition bar beside a time-axis chart).
export function Well({ children, className = "" }) {
  return <div className={`rounded-[10px] bg-surface p-3 ${className}`}>{children}</div>;
}

// Brand / restaurant marker beside a food name (note 60). Small caps in the quietest
// ink so the NAME stays what you read at a glance; a brand typed into the name was the
// thing that made the picker hard to scan on a phone. Renders nothing when empty.
export function BrandTag({ brand, className = "" }) {
  if (!brand) return null;
  return (
    <span className={`ml-1.5 whitespace-nowrap align-baseline text-[10px] font-medium uppercase tracking-micro text-ink-3 ${className}`}>
      {brand}
    </span>
  );
}

// Rho's mark (2026-10-29): the coach's identity wherever she speaks — coach card, class
// board. A ρ in a ring, structure ink only; it never carries state colour (the card's own
// chips do that). Deliberately not a face yet — avatar/personality come later.
export function RhoMark({ size = 14, className = "" }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" aria-label="Rho"
      className={`inline-block flex-none align-[-2px] ${className}`}>
      <circle cx="8" cy="8" r="7.25" fill="none" stroke="var(--ink-3)" strokeWidth="1" />
      <text x="8" y="11.4" textAnchor="middle" fontSize="10.5" fontWeight="600"
        fontFamily="ui-serif, Georgia, serif" fill="var(--ink)">ρ</text>
    </svg>
  );
}

// In-chart section label. Used above a sub-band inside a figure.
export function MicroLabel({ children, className = "" }) {
  return (
    <p className={`text-[8.5px] font-semibold tracking-micro text-ink-3 ${className}`}>
      {children}
    </p>
  );
}

// What a data section shows instead of unmounting (note 53). A section that simply
// vanished when its rows came back empty made a broken pipeline — or a bug in the
// chart maths, as with SCREEN TIME on 2026-09-23 — look exactly like a feature that
// was never built. So the three states that used to collapse into "nothing" are kept
// apart: still loading, the request failed, and genuinely nothing in the window.
// `query` is a TanStack result (isLoading/isError); `window` names the span so
// "nothing" is always "nothing in <when>", never an unqualified absence. `message`
// replaces that sentence when the reason is more specific than "nothing" (e.g. the
// server's own `reason`, or "only 3 days — needs 7").
export function EmptyState({ query, window, what = "Nothing logged", hint = null, message = null, className = "" }) {
  const failed = query?.isError;
  const loading = !failed && query?.isLoading;
  return (
    <p className={`mt-1.5 text-[10px] leading-snug ${failed ? "text-[color:var(--over)]" : "text-ink-3"} ${className}`}>
      {failed ? "Couldn't load — the request failed. This is not an empty result."
        : loading ? "Loading…"
        : <>{message ?? `${what} in ${window}.`}{hint && <> {hint}</>}</>}
    </p>
  );
}
