export default function MetricInfoModal({ metric, onClose }) {
  if (!metric) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/60 pb-16" onClick={onClose}>
      <div
        className="w-full max-w-md rounded-t-2xl border-t border-rule bg-card p-6 pb-10"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-start justify-between">
          <h2 className="text-[17px] font-semibold text-ink">{metric.name}</h2>
          <button onClick={onClose} className="text-xl leading-none text-ink-3 hover:text-ink">✕</button>
        </div>
        <p className="prose-ui mb-4 text-[13px] leading-relaxed text-ink-2">{metric.description}</p>
        {metric.target && (
          <div className="rounded-lg bg-surface p-3">
            <p className="mb-1 text-[9.5px] font-semibold tracking-eyebrow text-ink-3">TARGET</p>
            <p className="prose-ui text-[13px] text-ink">{metric.target}</p>
          </div>
        )}
      </div>
    </div>
  );
}

export const METRIC_INFO = {
  readiness: {
    name: "Recovery Score",
    description: "A composite 0–100 score of overnight recovery: HRV z-score (45%), resting HR z-score (25%), and sleep debt (30%), each normalized to 0–100. It deliberately measures recovery only — training load (ACWR) was removed from the score in July 2026 because load is a different construct, and in this user's own data higher recent load predicted BETTER next-day energy. Weights are heuristic and pre-registered for an empirical refit once ~150 finer-grained check-ins accumulate.",
    target: "75+ = High (well recovered) · 50–74 = Moderate · 25–49 = Low · <25 = Rest Day (prioritize recovery). Load context lives in the Workload row below.",
  },
  hrv: {
    name: "HRV — Heart Rate Variability",
    description: "Measures the variation in time between heartbeats. Higher HRV indicates your nervous system is well-recovered and adaptable. The z-score shown compares today's reading to your personal 30-day baseline — so it's relative to you, not a population average.",
    target: "Positive z-score = above your baseline = good. Aim for z > 0. Values below −1.5 suggest significant fatigue or illness.",
  },
  rhr: {
    name: "RHR — Overnight Resting Heart Rate",
    description: "Your low, sustained heart rate while asleep: the 10th percentile of last night's heart-rate readings. Lower is generally better. An elevated RHR (vs your own baseline) often signals incomplete recovery, alcohol, dehydration, or oncoming illness. It replaced Apple's daily resting HR in Oct 2026 — Apple computes that from the whole day, so it wasn't ready at wake-up and barely registered a night of drinking. Runs ~8 bpm below Apple's figure by construction. The z-score compares today to your 30-day baseline of the same overnight measure.",
    target: "Negative z-score = below your baseline = good. Values above +1.5 are a warning sign.",
  },
  sleep_debt: {
    name: "Sleep Debt",
    description: "The average nightly sleep deficit over the past 14 nights compared to your 7.5h target. Calculated as a rolling average rather than a cumulative sum — based on research showing sleep debt accumulates over ~2 weeks before becoming chronic.",
    target: "0h = meeting your sleep target on average. Below 0.5h/night is good. Above 1h/night = meaningful chronic deficit affecting recovery and performance.",
  },
  trimp: {
    name: "TRIMP — Training Impulse",
    description: "The single load currency this app uses. TRIMP scores a workout by how hard AND how long it was, from heart rate: TRIMP = Σ Δt · HRr · 0.64 · e^(1.92·HRr), where HRr is your heart-rate reserve — where you sat between resting and max HR. The exponential means intensity counts for more than duration, which is why a 20-minute interval session can outweigh a 60-minute walk. It is computed per HEART-RATE SAMPLE rather than from an average, because the curve is convex: averaging HR first and then applying the exponent understates variable-intensity sessions. ATL, CTL and ACWR are all built from it.",
    target: "No target — it is a unit, not a goal. A typical gym session is worth roughly 75–80 TRIMP (the Training tab shows your current figure; walks score far less). Two caveats: it is an HR-based measure, so it structurally under-weights pure resistance work (tonnage covers that), and a workout recorded WITHOUT heart rate scores zero regardless of effort.",
  },
  tonnage: {
    name: "Tonnage",
    description: "Total external load lifted: weight × reps, summed across working sets, warm-ups excluded. This is the counterpart to TRIMP — where TRIMP measures internal cardiovascular cost, tonnage measures mechanical work, and resistance training barely registers in the former. Sourced from Hevy set-level data.",
    target: "No absolute target; it depends entirely on movement selection. Watch the trend and compare like with like — a deadlift day and an accessory day are not comparable in tonnage.",
  },
  e1rm: {
    name: "e1RM — Estimated One-Rep Max",
    description: "An estimate of the most you could lift once, derived from a set you actually did, using the Epley formula: 1RM ≈ weight × (1 + reps/30). Taken from each session's best set of 15 reps or fewer. The estimate gets rougher as reps climb, because performance at high rep counts depends more on conditioning than on maximal strength, so a 15-rep set gives a less reliable number than a 5-rep set. Band-assisted movements are left out, since their logged weight is the assistance.",
    target: "The window-over-window change is the signal, not any single session. Session-to-session wobble of a few percent is normal and reflects fatigue, sleep and rep quality rather than real strength change.",
  },
  hard_sets: {
    name: "Hard Sets per Muscle",
    description: "Weekly count of working sets taken close to failure, attributed per muscle group. A set where a muscle is the primary mover counts fully; where it assists, it counts half. This is the volume measure most closely tied to hypertrophy in the training literature, and it is a better guide than tonnage because it is comparable across exercises. Building sets count only work that can grow muscle: sets with weight, plus bodyweight strength moves like push-ups and pull-ups. Conditioning, core circuits, carries and holds stay under All sets.",
    target: "Roughly 10–20 hard sets per muscle per week for growth. Below that is maintenance; well above it tends to accumulate fatigue faster than it adds stimulus. Region totals are not scored against this band — the guideline is per muscle, and regions contain different numbers of muscles.",
  },
  acwr: {
    name: "ACWR — Acute:Chronic Workload Ratio",
    description: "Compares your recent training load (7-day EWMA, called ATL) to your long-term base (42-day EWMA, called CTL). The ratio tells you whether you're doing more or less than your body is adapted to handle. Shown here for training planning — it is deliberately NOT part of the Recovery score (it measures load, not recovery).",
    target: "Your normal range is 0.8–2.0. Rho eases effort above 2.0 and keeps the day easy above 2.5; a light week below 0.8 is fine. The textbook 0.8–1.3 'sweet spot' assumes a smoothed ratio; yours has an SD of about 0.7, so 1.3–2.0 is an ordinary week for you.",
  },
  atl: {
    name: "ATL — Acute Training Load",
    description: "A 7-day exponentially weighted moving average of your daily training stress (measured as Banister TRIMP — an HR-based internal-load score from your per-second heart rate during workouts). Represents your current acute fatigue — what you've done recently.",
    target: "No universal target — higher means more recent work. Compare to your own CTL. ATL rising faster than CTL → ACWR climbing.",
  },
  ctl: {
    name: "CTL — Chronic Training Load",
    description: "A 42-day exponentially weighted moving average of training stress. Represents your base fitness level — the accumulated aerobic and strength adaptation from consistent training. Slow to build, slow to decay.",
    target: "Higher is better, but build gradually. A sharp drop means detraining. This is your 'fitness bank balance'.",
  },
  vo2max: {
    name: "VO₂ Max",
    description: "Maximum oxygen uptake in ml/kg/min — the gold standard measure of aerobic fitness. Reflects how efficiently your cardiovascular system can deliver oxygen to working muscles. Apple Watch estimates it only from an outdoor WALK, RUN or HIKE with GPS and continuous heart rate, at a brisk sustained effort for 20+ minutes. Interval work such as HIIT never qualifies, however hard it is, because the model needs steady-state HR. A gap in this chart usually means no qualifying session — or a walk recorded without heart rate — rather than unchanged fitness. Improves with consistent aerobic training and declines with inactivity or aging (roughly 1% per year after 25).",
    target: "Age and sex dependent. General adult benchmarks: <35 = poor, 35–45 = average, 45–55 = good, >55 = excellent. The chart trend matters more than the absolute number — a rising trend means improving aerobic fitness.",
  },
  exercise_minutes: {
    name: "Exercise Minutes",
    description: "Minutes of intentional exercise at or above a brisk-walk intensity (roughly ≥3 METs), as measured by Apple Watch. Counts time in workouts plus spontaneous movement that meets the threshold. The WHO recommends 150–300 minutes of moderate-intensity aerobic activity per week — about 21–43 minutes per day.",
    target: "Daily goal is configurable in Settings (default 30 min). Weekly target: 150+ min moderate or 75+ min vigorous intensity.",
  },
  stand_hours: {
    name: "Stand Hours",
    description: "Number of hours in the day where you stood and moved around for at least 1 minute, as tracked by Apple Watch. Designed to break up prolonged sitting, which is independently associated with metabolic and cardiovascular risk even in people who exercise regularly.",
    target: "Apple's goal is 12 hours per day. Hitting 8–10 hours is already a meaningful improvement over a fully sedentary day.",
  },
};
