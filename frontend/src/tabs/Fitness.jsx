import { useState, useEffect } from "react";
import { api } from "../api.js";
import { SegmentedTabs } from "../components/ui.jsx";
import Cardio from "./Cardio.jsx";
import Strength from "./Strength.jsx";
import Recovery from "./Recovery.jsx";
import Sleep from "./Sleep.jsx";

/**
 * Training and Recovery as two views of one tab.
 *
 * WHY THIS IS NOT THE SAME MOVE AS TODAY'S SPLIT
 * ----------------------------------------------
 * Today was SPLIT because it mixed two intents — glancing at state vs recording
 * input — and separating them made each half shorter. Training and Recovery are
 * both review views, so merging them saves no scrolling and costs a tap. On its
 * own, a toggle here would be strictly worse than two nav entries.
 *
 * It earns its place only because these two are halves of a single question:
 * load goes in, adaptation comes out. So the merge carries a SHARED HEADER that
 * shows both states at once; the toggle only switches which side you're reading
 * in detail. The header is the reason the tab exists — without it this is just
 * navigation with extra steps.
 *
 * Deliberately NOT asserted here: that load explains recovery. ACWR was removed
 * from the readiness score in July 2026 precisely because its component pointed
 * the wrong way for this user. The header co-locates the two and says what each
 * is; any causal reading is the experiment framework's job, not a caption's.
 *
 * THE HEADER WAS REMOVED 2026-11-04. With four views it no longer paired two halves; its
 * readiness repeated Today and the Recovery view's first card, its ACWR repeated the Cardio load
 * card, and its combined sentence appeared on 4 of the 90 days to 11-04. The tab is now
 * navigation between four views, which is all it needs to be.
 *
 * Training became CARDIO and STRENGTH on 2026-11-02. Those two are capacity, which moves
 * over months; RECOVERY and SLEEP are state, which moves daily. A metric lives where its question
 * is: HR recovery and load are cardio, the same HRV and RHR are recovery.
 */

const VIEWS = ["cardio", "strength", "recovery", "sleep"];

export default function Fitness({ window = "3M", onWindowChange, windowOffset = 0, onWindowOffsetChange, initialView = "cardio" }) {
  // Deep links matter here: the Today sleep card and the battery card both call
  // onNavigate("recovery"), and landing them on the Cardio view would be a
  // silent regression of a link that used to work. App keys this component on the
  // tab id so a nav from one of those cards remounts on the right side.
  // Old "training" links land on Cardio, which keeps the load chart that opened that view.
  const [view, setView] = useState(VIEWS.includes(initialView) ? initialView : "cardio");
  useEffect(() => { api.logView(`fitness/${view}`); }, [view]);
  return (
    <div>
      <div className="px-4 pt-4">
        <SegmentedTabs
          value={view} onChange={setView}
          options={[
            { value: "cardio",   label: "CARDIO" },
            { value: "strength", label: "STRENGTH" },
            { value: "recovery", label: "RECOVERY" },
            { value: "sleep",    label: "SLEEP" },
          ]}
        />
      </div>

      {/* The child tabs own their own padding and window selector. */}
      {view === "cardio"   && <Cardio   window={window} onWindowChange={onWindowChange} windowOffset={windowOffset} onWindowOffsetChange={onWindowOffsetChange} />}
      {view === "strength" && <Strength window={window} onWindowChange={onWindowChange} windowOffset={windowOffset} onWindowOffsetChange={onWindowOffsetChange} />}
      {view === "recovery" && <Recovery window={window} onWindowChange={onWindowChange} windowOffset={windowOffset} onWindowOffsetChange={onWindowOffsetChange} />}
      {view === "sleep"    && <Sleep    window={window} onWindowChange={onWindowChange} windowOffset={windowOffset} onWindowOffsetChange={onWindowOffsetChange} />}
    </div>
  );
}
