/**
 * Verification for FlagBands' run-collapsing — the only real logic in that component.
 *
 * There is no frontend test runner here (backend/tests are standalone scripts too), so
 * this bundles itself through the esbuild that ships with vite. From `frontend/`:
 *
 *   npx esbuild tests/verify_flag_bands.mjs --bundle --platform=node --format=esm \
 *     --outfile=/tmp/fb.mjs --loader:.jsx=jsx --jsx=automatic && node /tmp/fb.mjs
 *
 * It imports the REAL function rather than a copy, so it cannot drift from shipped code.
 */
import { collapseFlagRuns } from "../src/components/FlagBands.jsx";
const eq = (a, b, msg) => {
  const ok = JSON.stringify(a) === JSON.stringify(b);
  console.log(`${ok ? "PASS" : "FAIL"}  ${msg}`);
  if (!ok) { console.log("   got     ", JSON.stringify(a)); console.log("   expected", JSON.stringify(b)); }
  return ok;
};
let all = true;
all &= eq(collapseFlagRuns([
  {date:"2026-03-02",reason:"sick"},{date:"2026-03-03",reason:"sick"},
  {date:"2026-03-04",reason:"sick"},{date:"2026-03-05",reason:"sick"}]),
  [{reason:"sick",startIso:"2026-03-02",endIso:"2026-03-05",days:4}],
  "four consecutive sick days merge into one 4-day run");

all &= eq(collapseFlagRuns([
  {date:"2026-03-02",reason:"sick"},{date:"2026-03-05",reason:"sick"}]),
  [{reason:"sick",startIso:"2026-03-02",endIso:"2026-03-02",days:1},
   {reason:"sick",startIso:"2026-03-05",endIso:"2026-03-05",days:1}],
  "a gap splits the run");

all &= eq(collapseFlagRuns([
  {date:"2026-03-02",reason:"sick"},{date:"2026-03-03",reason:"travel"}]),
  [{reason:"sick",startIso:"2026-03-02",endIso:"2026-03-02",days:1},
   {reason:"travel",startIso:"2026-03-03",endIso:"2026-03-03",days:1}],
  "different reasons never merge across each other");

all &= eq(collapseFlagRuns([{date:"2026-08-01",reason:"incomplete_data"}]), [],
  "incomplete_data excluded by default");
all &= eq(collapseFlagRuns([{date:"2026-08-01",reason:"incomplete_data"}], {includeIncomplete:true}),
  [{reason:"incomplete_data",startIso:"2026-08-01",endIso:"2026-08-01",days:1}],
  "incomplete_data included on request");

all &= eq(collapseFlagRuns([{date:"2026-03-03",reason:"sick"},{date:"2026-03-02",reason:"sick"}]),
  [{reason:"sick",startIso:"2026-03-02",endIso:"2026-03-03",days:2}],
  "unsorted input still merges correctly");
all &= eq(collapseFlagRuns([{date:"2026-03-02",reason:"sick"},{date:"2026-03-02",reason:"sick"}]),
  [{reason:"sick",startIso:"2026-03-02",endIso:"2026-03-02",days:1}],
  "duplicate rows for one day count once");

// month boundary — Feb 28 -> Mar 1 2026 (not a leap year)
all &= eq(collapseFlagRuns([{date:"2026-02-28",reason:"travel"},{date:"2026-03-01",reason:"travel"}]),
  [{reason:"travel",startIso:"2026-02-28",endIso:"2026-03-01",days:2}],
  "run spans a month boundary");
// REGRESSION: /api/day-flags returns an envelope, not a bare array. Passing that
// object straight in threw "{} is not iterable" and took down the Body tab.
all &= eq(collapseFlagRuns({flags:[{date:"2026-03-02",reason:"sick"},
                                   {date:"2026-03-03",reason:"sick"}],
                            reasons:["sick"], labels:{sick:"Sick"}}),
  [{reason:"sick",startIso:"2026-03-02",endIso:"2026-03-03",days:2}],
  "accepts the {flags,reasons,labels} API envelope");
all &= eq(collapseFlagRuns({flags:[]}), [], "empty envelope");
all &= eq(collapseFlagRuns({}), [], "envelope with no flags key");

all &= eq(collapseFlagRuns([]), [], "empty input");
all &= eq(collapseFlagRuns(undefined), [], "undefined input");
console.log(all ? "\nALL PASS" : "\nFAILURES");
process.exit(all ? 0 : 1);
