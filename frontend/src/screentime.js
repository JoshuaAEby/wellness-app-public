// Shared screen-time category colours/labels + minute formatting.
// Category order is also the stacking order in charts.
export const ST_CATS = [
  "social", "communication", "ai", "entertainment", "learning", "productivity",
  "navigation", "health", "system", "other",
];

export const ST_COLOR = {
  social:        "#ec4899", // pink
  communication: "#38bdf8", // sky
  ai:            "#f59e0b", // amber
  entertainment: "#a78bfa", // violet
  learning:      "#6366f1", // indigo
  productivity:  "#22c55e", // green
  navigation:    "#14b8a6", // teal
  health:        "#f43f5e", // rose
  system:        "#64748b", // slate
  other:         "#94a3b8", // gray
};

export const ST_LABEL = {
  social:        "Social",
  communication: "Comms",
  ai:            "AI",
  entertainment: "Entertainment",
  learning:      "Learning",
  productivity:  "Productivity",
  navigation:    "Navigation",
  health:        "Health",
  system:        "System",
  other:         "Other",
};

export function fmtMin(m) {
  m = Math.round(m || 0);
  if (m >= 60) {
    const h = Math.floor(m / 60), mm = m % 60;
    return mm ? `${h}h ${mm}m` : `${h}h`;
  }
  return `${m}m`;
}
