import { useQuery } from "@tanstack/react-query";
import { api } from "../api.js";
import { Card, Eyebrow, HeroSub, K, Sep, Foot, EmptyState } from "./ui.jsx";

/**
 * The coverage tripwire — silent unless something changed.
 *
 * Reads DRIFT against your own history, never an RDA. The reference is what you
 * normally eat, which avoids inventing quantities: "no vitamin-A source logged in
 * 22 days" is checkable against stored data, "47% of the RDA" would need portion
 * mass, nutrient concentration and bioavailability, none of which exist here.
 *
 * Levels are shown alongside deltas deliberately. A tripwire is blind to whatever
 * never changed — a group absent for the whole record produces no delta at all — so
 * every group's current rate stays on screen whether or not it fired.
 *
 * Two guards, both learned from a false positive this design had to survive:
 *   - Nothing raises an alarm unless enough of the window is classified. In a system
 *     whose value is silence, a false alarm costs more than a missed one.
 *   - A group at exactly zero is reported as "none logged", never as "you stopped
 *     eating this". Logged coffee fell to 0% once caffeine moved to the intake
 *     logger; a hard zero can be a channel change rather than a dietary one.
 */

const TONE = {
  dropped: "var(--over)",
  absent: "var(--over)",
  never: "var(--ink-3)",
  accruing: "var(--ink-3)",
  sparse: "var(--ink-3)",
  ok: "var(--ink-2)",
  no_data: "var(--ink-3)",
};

function statusNote(g) {
  switch (g.status) {
    case "dropped":
      return `down from ${g.baseline_pct}% (${g.baseline_days_with}d)`;
    case "absent":
      return g.days_since_last != null
        ? `none in ${g.days_since_last} days`
        : "none logged recently";
    case "never":
      return "never logged";
    case "accruing":
      return "building a baseline";
    case "sparse":
      // Named rather than hidden: the level is the finding here, and a group this
      // rare has no habit to lapse, so it must not read as a fresh alarm.
      return `rare — ${g.baseline_days_with}d in baseline`;
    default:
      return g.days_since_last != null && g.days_since_last > 6
        ? `last ${g.days_since_last}d ago`
        : null;
  }
}

function Row({ g }) {
  const pct = g.recent_pct ?? 0;
  const flagged = g.status === "dropped" || g.status === "absent";
  const note = statusNote(g);
  return (
    <div className="flex items-center gap-2.5 py-[3px]">
      <span className="w-[104px] flex-none truncate text-[10.5px] text-ink-2">{g.label}</span>
      {/* The track is the row: a share reads faster as a length than as a number,
          and the baseline tick shows what "normal" was without a second chart. */}
      <span className="relative h-[9px] flex-1 overflow-hidden rounded-[2px] bg-surface">
        <span
          className="absolute inset-y-0 left-0 rounded-[2px]"
          style={{
            width: `${Math.min(100, pct)}%`,
            background: flagged ? "var(--over)" : "var(--food-veg)",
            opacity: flagged ? 0.85 : 0.6,
          }}
        />
        {g.baseline_pct != null && (
          <span
            className="absolute inset-y-0 w-px"
            style={{ left: `${Math.min(100, g.baseline_pct)}%`, background: "var(--ink-2)", opacity: 0.75 }}
            title={`baseline ${g.baseline_pct}%`}
          />
        )}
      </span>
      <span className="w-[34px] flex-none text-right text-[10.5px] tabular-nums"
        style={{ color: flagged ? "var(--over)" : "var(--ink)" }}>
        {g.recent_pct != null ? `${Math.round(g.recent_pct)}%` : "—"}
      </span>
      <span className="w-[124px] flex-none truncate text-[9.5px]" style={{ color: TONE[g.status] }}>
        {note}
      </span>
    </div>
  );
}

export default function FoodCoverage() {
  const q = useQuery({
    queryKey: ["foodCoverage"],
    queryFn: () => api.getFoodCoverage(),
    staleTime: 1000 * 60 * 30,
  });
  const { data } = q;

  const alerts = data?.alerts ?? [];
  const groups = (data?.groups ?? []).filter((g) => g.status !== "no_data");
  // "Silent unless something changed" is about ALARMS, not the card: with nothing to
  // show it used to unmount, which is indistinguishable from the tagger having died
  // (note 53). The tripwire's silence only means something if the card is visibly there.
  if (!groups.length) {
    return (
      <Card>
        <Eyebrow left="FOOD-GROUP COVERAGE" />
        <EmptyState query={q} message={
          data && !data.groups?.length
            ? "No food-group tags yet — entries are tagged in the background after they are logged."
            : data ? "No food group has enough tagged days yet to compare against your own history."
            : null} />
      </Card>
    );
  }

  return (
    <Card>
      <Eyebrow
        left="FOOD-GROUP COVERAGE"
        right={alerts.length ? `${alerts.length} FLAGGED` : "NOTHING FLAGGED"}
      />

      {alerts.length > 0 ? (
        <HeroSub className="!text-ink">
          {alerts.map((a) => a.label).join(", ")} —{" "}
          {alerts.length === 1 ? "this has" : "these have"} moved against your own
          baseline.
          {/* The channel-change caveat belongs only to a hard zero. A partial
              decline is the shape a real dietary change takes, and attaching the
              hedge to it would train you to discount every alert. */}
          {alerts.some((a) => a.status === "absent") && (
            <> A group at zero can also mean the food started being logged somewhere
            else, so check that before reading it as a change in what you ate.</>
          )}
        </HeroSub>
      ) : (
        <HeroSub>
          Nothing has shifted against your own baseline. This card stays quiet unless
          a group moves — it reads change, not adequacy, so a level that was always low
          will never trip it.
        </HeroSub>
      )}

      <div className="mt-3.5">
        {groups.map((g) => <Row key={g.key} g={g} />)}
      </div>

      {!data.reliable && (
        <p className="prose-ui mt-3 text-[10.5px] leading-snug" style={{ color: "var(--over)" }}>
          Only {data.tagged_entry_pct}% of entries in this window are classified
          (needs {data.min_tagged_pct}%). Rates below are partial and no alarm will be
          raised until the food library is more fully tagged.
        </p>
      )}

      <Foot>
        Bars are the share of the last <K>{data.recent_days}</K> logged days containing
        a source; the tick is your <K>{data.baseline_days}</K>-day baseline.
        <Sep />
        Presence only — no attempt is made to estimate how much of a nutrient a portion
        contained, which would need portion mass and bioavailability the log doesn’t have.
      </Foot>
    </Card>
  );
}
