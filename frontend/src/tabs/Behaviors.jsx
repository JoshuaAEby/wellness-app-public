import Record from "./behaviors/Record.jsx";
import Holding from "./behaviors/Holding.jsx";
import Exposures from "./behaviors/Exposures.jsx";
import NotEvaluable from "./behaviors/NotEvaluable.jsx";
import Context from "./behaviors/Context.jsx";
import Regimen from "./behaviors/Regimen.jsx";

/**
 * Behaviours — the levers you can pull, what you actually did with them, and
 * which ones have been shown to move anything.
 *
 * Renamed from "Habits" deliberately. "Habit" implies something you are trying
 * to build, which makes a behaviour sitting at 0% read as a personal failure
 * rather than a fact; it also has no natural room for alcohol or screen time,
 * which are among the best-evidenced behaviours here. "Behaviour" is
 * descriptive and holds all of them without a moral frame.
 *
 * Build order is deliberate — cards land here as they are converted, so the tab
 * stays shippable throughout rather than being dark until the whole rebuild is
 * done. All five cards are in place.
 */
export default function Behaviors() {
  return (
    <div className="space-y-4 p-4">
      <Record />
      <Holding />
      <Exposures />
      <Context />
      <NotEvaluable />
      <Regimen />
    </div>
  );
}
