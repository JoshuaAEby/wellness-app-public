// Minimal, deliberately: ONE rule that the build genuinely cannot replace.
//
// WHY THIS EXISTS (2026-09-11). `npm run build` bundles without undefined-variable
// analysis, so a ReferenceError ships as a green build. That happened: a multi-part
// edit to IntakeTrio landed the JSX that USED `categories` but not the block that
// DEFINED it, the build passed, and the alcohol sheet threw on open. Nothing in the
// toolchain would have caught it — and the app is behind a self-signed cert and a login
// wall, so it cannot be clicked from a dev machine either.
//
// Scope is intentionally narrow. This is not a style gate and should not grow into one;
// the codebase has its own conventions and a formatter fight would just get the script
// disabled. Add a rule here only when it catches something a build cannot.
import reactHooks from "eslint-plugin-react-hooks";

export default [
  {
    files: ["src/**/*.{js,jsx}"],
    // The existing `react-hooks/exhaustive-deps` disable comments read as "unused" only
    // because that rule is off above. Removing the comments would be wrong — they are
    // correct and load-bearing the moment the rule is enabled — so the report is
    // silenced instead. A lint that always prints something gets ignored.
    linterOptions: { reportUnusedDisableDirectives: "off" },
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      parserOptions: { ecmaFeatures: { jsx: true } },
      globals: {
        // Browser + standard globals actually used in this codebase. Listed explicitly
        // rather than pulled from a `globals` package so the dependency stays at one.
        window: "readonly", document: "readonly", navigator: "readonly",
        console: "readonly", fetch: "readonly", Response: "readonly",
        localStorage: "readonly", sessionStorage: "readonly",
        setTimeout: "readonly", clearTimeout: "readonly",
        setInterval: "readonly", clearInterval: "readonly",
        requestAnimationFrame: "readonly", cancelAnimationFrame: "readonly",
        ResizeObserver: "readonly", IntersectionObserver: "readonly",
        URL: "readonly", URLSearchParams: "readonly", Blob: "readonly",
        File: "readonly", FormData: "readonly", Image: "readonly",
        AbortController: "readonly", performance: "readonly", crypto: "readonly",
        alert: "readonly", confirm: "readonly", prompt: "readonly",
        Date: "readonly", Math: "readonly", JSON: "readonly", Intl: "readonly",
        Number: "readonly", String: "readonly", Boolean: "readonly",
        Array: "readonly", Object: "readonly", Map: "readonly", Set: "readonly",
        Promise: "readonly", Error: "readonly", isNaN: "readonly",
        parseFloat: "readonly", parseInt: "readonly", structuredClone: "readonly",
      },
    },
    // The codebase leans on `// eslint-disable-next-line react-hooks/exhaustive-deps`.
    // A disable comment naming a rule ESLint does not know is itself an error, so the
    // plugin is registered to make those comments resolve — with its rules OFF. Turning
    // exhaustive-deps on is a separate, deliberate decision; it would surface a wall of
    // findings unrelated to why this config exists.
    plugins: { "react-hooks": reactHooks },
    rules: {
      "no-undef": "error",
      "react-hooks/rules-of-hooks": "off",
      "react-hooks/exhaustive-deps": "off",
      // TRIED AND REMOVED: "no-use-before-define". It flags a module-level const used
      // inside a component declared above it (`GOLD` in behaviors/grid.jsx), which
      // resolves fine at call time — three false positives, zero real bugs. A linter
      // that cries wolf gets switched off, which would cost the one rule that works.
    },
  },
];
