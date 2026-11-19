// ACWR bands: one rule app-wide (2026-11-04). Mirrors backend/app/acwr.py, which explains
// the numbers: Rho only eases effort above 2.0, so a ratio of 1.3–2.0 is a normal week for him,
// not "overreaching". A light week (< 0.8) isn't a problem either; Rho treats it as fresh.
export const ACWR_LOW = 0.8;
export const ACWR_HIGH = 2.0;
export const ACWR_VERY_HIGH = 2.5;

export function acwrBand(acwr) {
  if (acwr == null) return null;
  if (acwr > ACWR_VERY_HIGH) return "very_high";
  if (acwr > ACWR_HIGH) return "high";
  if (acwr < ACWR_LOW) return "light";
  return "normal";
}

export const ACWR_LABEL = { light: "light", normal: "normal", high: "high", very_high: "very high" };
