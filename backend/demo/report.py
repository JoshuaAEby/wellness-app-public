"""Score the correlation engine against the demo's ground truth: python -m demo.report [db]

Reads the correlation cache the way the app does (`read_correlation_cache`, which
re-applies the BH correction), then sorts every surviving finding (significant or
suggestive) into:

  recovered   — a planted effect, on an input that measures the planted cause, with the
                right sign
  trap        — one of the two deliberate traps; it SHOULD carry the CONFOUNDED label
  co-occurrence — drink calories riding along with alcohol: spurious, and a kind of
                confounding the reverse-lag test cannot see
  wrong sign  — a planted pair found pointing the wrong way
  false       — anything else. The generator draws every other behaviour independently,
                so these are false positives (or a path the table below does not list —
                each is printed so it can be argued about)

and lists each planted effect the engine missed. Writes Markdown to stdout.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent


def _classify(r: dict, truth: list[dict]):
    a, b, rho = r["metric_a"], r["metric_b"], r.get("spearman_r") or 0.0
    for t in truth:
        if b == t["outcome"] and a in t["inputs"]:
            expected = t["inputs"][a]          # the sign this input should carry
            right = (rho > 0) == (expected > 0)
            if t["kind"] == "planted":
                return ("recovered" if right else "wrong sign"), t
            if t["kind"] == "co-occurrence":
                return "co-occurrence", t
            return "trap", t
    return "false", None


async def report() -> str:
    from demo.model import GROUND_TRUTH
    from app.services.correlations import read_correlation_cache, reverse_check_label

    rows = await read_correlation_cache()
    tested = [r for r in rows if r.get("p_value") is not None and not str(r["metric_a"]).startswith("ctrl_")]
    survivors = [r for r in rows if (r.get("significant") or r.get("suggestive"))]
    out, hit = [], set()
    buckets = {"recovered": [], "trap": [], "co-occurrence": [], "wrong sign": [], "false": [], "control": []}
    for r in survivors:
        if str(r["metric_a"]).startswith("ctrl_"):
            buckets["control"].append((r, None))
            continue
        kind, t = _classify(r, GROUND_TRUTH)
        buckets[kind].append((r, t))
        if t is not None and kind == "recovered":
            hit.add(t["id"])

    def line(r, t=None):
        tier = "significant" if r.get("significant") else "suggestive"
        label = reverse_check_label(r) or "—"
        why = f" — {t['what']}" if t else ""
        return (f"| `{r['metric_a']}` → `{r['metric_b']}` | {r['lag_days']} | {r['spearman_r']:+.2f} | "
                f"{r['n']} | {tier} | {label} |{why}")

    planted = [t for t in GROUND_TRUTH if t["kind"] == "planted"]
    out.append("# Demo data: planted effects vs what the engine found\n")
    out.append(f"Pairs tested: {len(tested)}. Surviving (significant or suggestive): {len(survivors)}.\n")
    out.append(f"- Planted effects recovered: **{len(hit)} of {len(planted)}**")
    out.append(f"- Trap pairs surviving: {len(buckets['trap'])} "
               f"(labelled CONFOUNDED: {sum(1 for r, _ in buckets['trap'] if reverse_check_label(r) == 'confounded')})")
    out.append(f"- Co-occurrence survivors (drink calories riding along with alcohol): {len(buckets['co-occurrence'])}")
    out.append(f"- Wrong-sign survivors: {len(buckets['wrong sign'])}")
    out.append(f"- False positives (no path in the generator): {len(buckets['false'])}")
    out.append(f"- Negative controls surviving: {len(buckets['control'])}\n")
    head = "| pair | lag | ρ | n | tier | reverse-lag check |\n|---|---|---|---|---|---|"
    for name in ("recovered", "trap", "co-occurrence", "wrong sign", "false", "control"):
        if buckets[name]:
            out.append(f"## {name.capitalize()}\n\n{head}")
            out.extend(line(r, t) for r, t in sorted(buckets[name], key=lambda x: -abs(x[0]['spearman_r'])))
            out.append("")
    traps = [t for t in GROUND_TRUTH if t["kind"] in ("confounded", "reverse")]
    trap_hit = {t["id"] for _, t in buckets["trap"]}
    quiet = [t for t in traps if t["id"] not in trap_hit]
    if quiet:
        out.append("## Traps that produced no surviving pair\n")
        out.extend(f"- {t['what']} (`{'`, `'.join(t['inputs'])}` → `{t['outcome']}`)" for t in quiet)
        out.append("")
    missed = [t for t in planted if t["id"] not in hit]
    if missed:
        out.append("## Planted but not found\n")
        for t in missed:
            best = sorted((r for r in tested if r["metric_b"] == t["outcome"] and r["metric_a"] in t["inputs"]),
                          key=lambda r: -abs(r.get("spearman_r") or 0))[:1]
            extra = (f" Strongest tested pair: `{best[0]['metric_a']}` lag {best[0]['lag_days']}, "
                     f"ρ {best[0]['spearman_r']:+.2f}, n {best[0]['n']}.") if best else " Never tested."
            out.append(f"- **{t['what']}** ({t['size']}).{extra}")
    return "\n".join(out) + "\n"


def main() -> None:
    db = Path(sys.argv[1] if len(sys.argv) > 1 else BACKEND / "data" / "demo.db").resolve()
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db}"
    sys.path.insert(0, str(BACKEND))
    print(asyncio.run(report()))


if __name__ == "__main__":
    main()
