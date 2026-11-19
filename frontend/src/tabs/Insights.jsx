import { useState, useEffect } from "react";
import { SegmentedTabs } from "../components/ui.jsx";
import { api } from "../api.js";
import Findings from "./insights/Findings.jsx";
import Experiments from "./insights/Experiments.jsx";
import Progress from "./insights/Progress.jsx";

/**
 * Insights, split by the KIND OF CLAIM each view makes.
 *
 * The old Trends tab put three different epistemic categories at similar visual
 * weight, so a rule-based digest line, an FDR-corrected correlation and a
 * randomised trial all read as equally solid. They are not, and the difference is
 * exactly what determines whether you should act:
 *
 *   ANALYSIS     — what happened. Descriptive, no inference. Digest, trends,
 *                  month-over-month, interventions, AI summaries (TRENDS), and
 *                  whether the inputs are fresh (DATA HEALTH). Was SIGNALS, with Data
 *                  Health buried at the top of it; folded into one area 2026-10-21
 *                  (backlog #13) and moved last — it is the least-visited view.
 *   FINDINGS     — what co-occurs, and how much to trust it. Correlations with
 *                  their denominator, synthetic controls, prospective holdout.
 *                  Co-occurrence only; never a cause.
 *   EXPERIMENTS  — what has actually been tested. Randomised, pre-registered
 *                  target, ITT. The only tier that licenses "do X because Y".
 *
 * FINDINGS → EXPERIMENTS is weakest-to-strongest evidence; ANALYSIS (descriptive)
 * sits at the end because usage put it there (a handful of visits a month).
 *
 * ANALYSIS WAS REMOVED 2026-11-04 (note 99: "pretty redundant"; ~10 visits ever).
 * What only it had moved: DATA HEALTH → Settings, marked changes (interventions) → the
 * bottom of EXPERIMENTS, TYPICAL → a collapsed "Your typical day" under FINDINGS. The
 * rest repeated other screens (weekly digest = the weekly summary, month-over-month and
 * What's Moving = Progress) and went with tabs/Trends.jsx, as did four analyses (day of
 * week, workout-type contrast, threshold bands, corroboration) unrendered since 2026-08-19.
 * Their backend endpoints are untouched.
 */
export default function Insights() {
  // FINDINGS is the landing view: it answers "what does any of this mean", which is
  // the question the tab exists for. Worth knowing that it is also the SLOWEST view —
  // the correlation cache refreshes weekly, so most opens show identical content.
  // That is why Data Health (now in Settings) surfaces the cache age: unchanged numbers here
  // mean "not recomputed", not "nothing moved".
  const [view, setView] = useState("findings");
  useEffect(() => { api.logView(`insights/${view}`); }, [view]);
  return (
    <div>
      <div className="px-4 pt-4">
        <SegmentedTabs
          value={view} onChange={setView}
          options={[
            // PROGRESS (note 56): absolute change since training began — the one view
            // not relative to a rolling baseline. Listed first; landing stays FINDINGS.
            { value: "progress", label: "PROGRESS" },
            { value: "findings", label: "FINDINGS" },
            { value: "experiments", label: "EXPERIMENTS" },
          ]}
        />
      </div>
      {view === "progress" && <Progress />}
      {view === "findings" && <Findings />}
      {view === "experiments" && <Experiments />}
    </div>
  );
}
