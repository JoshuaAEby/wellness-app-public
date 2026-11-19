/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      // Token aliases so utilities (bg-card, text-ink-2, border-rule) resolve to
      // the CSS custom properties in index.css. One definition, two consumers:
      // Tailwind classes for layout chrome, var(--x) for SVG/inline chart fills.
      colors: {
        canvas:  "var(--canvas)",
        card:    "var(--card)",
        surface: "var(--surface)",
        rule:    "var(--rule)",
        ink:     { DEFAULT: "var(--ink)", 2: "var(--ink-2)", 3: "var(--ink-3)" },
        over:    "var(--over)",
        under:   "var(--under)",
      },
      fontFamily: {
        mono: ['"IBM Plex Mono"', "ui-monospace", "monospace"],
        ui:   ['"Space Grotesk"', "system-ui", "sans-serif"],
      },
      letterSpacing: {
        eyebrow: "0.2em",   // card eyebrows
        micro:   "0.16em",  // in-chart section labels
      },
      keyframes: {
        // Character-sheet level-up flourish. Deliberately slow and low-contrast —
        // a sweep of light across the badge, not a flashing arcade cabinet.
        sheen: {
          "0%":   { transform: "translateX(-130%)" },
          "55%":  { transform: "translateX(230%)" },
          "100%": { transform: "translateX(230%)" },
        },
        breathe: {
          "0%, 100%": { opacity: "0.5" },
          "50%":      { opacity: "1" },
        },
        // Indeterminate progress. Continuous and linear on purpose — `sheen` dwells
        // for ~45% of its cycle, which reads as "stalled" when it's standing in for
        // work still happening.
        indeterminate: {
          "0%":   { transform: "translateX(-100%)" },
          "100%": { transform: "translateX(400%)" },
        },
      },
      animation: {
        sheen: "sheen 3.4s ease-in-out infinite",
        breathe: "breathe 2.8s ease-in-out infinite",
        indeterminate: "indeterminate 1.6s linear infinite",
      },
    },
  },
  plugins: [],
};
