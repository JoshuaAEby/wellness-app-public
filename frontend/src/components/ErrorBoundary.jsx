import React from "react";

/**
 * Keeps one broken component from taking down the whole app.
 *
 * Added 2026-08-17 after the Behaviors rebuild shipped with an undefined
 * dereference on first render: because nothing caught it, a single bad card
 * rendered the ENTIRE app as a blank black screen — no header, no nav, no way to
 * navigate away or even tell what had happened. The failure was small; the blast
 * radius was total.
 *
 * Deliberate choices:
 *   • The error text is SHOWN, not hidden behind a generic apology. This is a
 *     single-user app and the user reads stack traces; "something went wrong" would
 *     throw away the only useful information at exactly the moment it is needed.
 *   • It still calls console.error, so React's own overlay and the browser console
 *     keep working. A boundary that swallows errors makes debugging harder than
 *     having no boundary at all.
 *   • `resetKey` remounts the boundary (via key= at the call site) so navigating to
 *     another tab clears the error rather than leaving a sticky failure.
 *   • "Try again" re-mounts the subtree without a full reload, which is usually
 *     enough when the cause was a transient undefined during loading.
 */
export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null, attempt: 0, showStack: false };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    // Keep the real diagnostics flowing — never swallow.
    console.error(`[${this.props.label ?? "component"}] render failed:`, error, info?.componentStack);
  }

  render() {
    const { error, showStack } = this.state;
    if (!error) return this.props.children;

    return (
      <div className="p-4">
        <div className="rounded-xl border border-rule bg-card p-4">
          <p className="text-[9px] tracking-eyebrow text-ink-3">
            {(this.props.label ?? "THIS VIEW").toUpperCase()} FAILED TO RENDER
          </p>
          <p className="mt-2 text-[13px] leading-snug text-ink">
            {String(error.message || error)}
          </p>
          <p className="mt-2 text-[10.5px] leading-snug text-ink-3">
            The rest of the app is unaffected — the tabs below still work. This is a
            bug in this view, not in your data.
          </p>

          <div className="mt-3 flex flex-wrap items-center gap-2">
            <button
              onClick={() => this.setState((s) => ({ error: null, attempt: s.attempt + 1 }))}
              className="rounded-md bg-surface px-2.5 py-1 text-[11px] text-ink-2 hover:text-ink">
              Try again
            </button>
            <button
              onClick={() => window.location.reload()}
              className="rounded-md bg-surface px-2.5 py-1 text-[11px] text-ink-2 hover:text-ink">
              Reload
            </button>
            {error.stack && (
              <button
                onClick={() => this.setState((s) => ({ showStack: !s.showStack }))}
                className="ml-auto text-[10px] text-ink-3 hover:text-ink-2">
                {showStack ? "hide details" : "details"}
              </button>
            )}
          </div>

          {showStack && error.stack && (
            <pre className="mt-2 max-h-56 overflow-auto rounded-md bg-canvas p-2 text-[9.5px] leading-snug text-ink-3">
              {error.stack}
            </pre>
          )}
        </div>
      </div>
    );
  }
}
