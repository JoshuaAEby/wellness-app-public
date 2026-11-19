"""Parse a lab PDF twice, independently, and reconcile the two readings.

## Why two parses

A blood panel is entered once and then trended for years. A misparsed value is
silent, permanent corruption of a series — nothing downstream can detect that ApoB
"140" was really "104", and there is no second draw to check it against. So instead
of picking one extraction path and trusting it, both run and their disagreements get
surfaced for human verification.

## Why THESE two arms

    text arm   pypdf text layer  -> Haiku 4.5
    pdf  arm   PDF document block -> Opus 5  (sees page images + text)

The arms differ in BOTH modality and model, which is the entire point: agreement
between them is real evidence, whereas two calls to the same model on the same input
would share their failure modes and agree confidently while both being wrong.

Measured on synthetic fixtures (card layout / table layout / rasterised scan, n=5):
neither arm ever produced a wrong *value*. What the arms did differ on was reference
ranges — Haiku read the axis tick "0" as ref_low for every one-sided range on the
card layout, 5/5 markers on 5/5 runs, while Opus made zero such errors. And the text
arm cannot read a scan at all (0 chars extracted).

That measurement is the reason severity is split: a value disagreement is loud, a
reference-range disagreement is quiet. Reference ranges are lab metadata, not the
measurement; getting one wrong mislabels a marker as in/out of range but doesn't
corrupt the trend.

## Degraded modes are labelled, never hidden

A scan has no text layer, so the text arm can't run and there is no cross-check. That
result is marked `single_source` rather than being quietly presented as if two
independent parses had agreed — "unverified" and "verified" must not look alike.
"""
import asyncio
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# Different arms may legitimately canonicalise the same analyte differently. Folding
# these together prevents one arm's naming choice from showing up as a bogus
# "missing in the other arm" row.
_ALIAS = {
    "cholesterol": "total_cholesterol", "cholesterol_total": "total_cholesterol",
    "total_cholesterol_calc": "total_cholesterol",
    "apolipoprotein_b": "apob", "apo_b": "apob",
    "ldl_cholesterol": "ldl_c", "ldl": "ldl_c", "ldl_calc": "ldl_c",
    "hdl_cholesterol": "hdl_c", "hdl": "hdl_c",
    # 2026-10-28 live screenshot check: Opus said vldl_c (the stored key), Sonnet
    # snake_cased the lab's "VLDL Cholesterol Cal" — a false "missing in one arm" pair.
    "vldl": "vldl_c", "vldl_cholesterol": "vldl_c", "vldl_cholesterol_cal": "vldl_c",
    "vldl_cholesterol_calc": "vldl_c", "vldl_chol_calc": "vldl_c",
    "lipoprotein_a": "lp_a", "lpa": "lp_a", "lp_little_a": "lp_a",
    "glucose": "fasting_glucose", "glucose_fasting": "fasting_glucose",
    "hemoglobin_a1c": "hba1c", "a1c": "hba1c", "hgb_a1c": "hba1c",
    "crp_hs": "hs_crp", "hscrp": "hs_crp", "c_reactive_protein_high_sensitivity": "hs_crp",
    "vitamin_d_25_oh": "vitamin_d", "vitamin_d_25_hydroxy": "vitamin_d",
    "alt_sgpt": "alt", "sgpt": "alt", "ast_sgot": "ast", "sgot": "ast",
    "hgb": "hemoglobin", "hct": "hematocrit",
    # Absolute differential counts (2026-10-28) — distinct markers from the % rows.
    **{f"{a}_{suf}": f"{k}_abs"
       for k, al in (("neutrophils", ("neutrophils", "neutrophil", "neuts")),
                     ("lymphocytes", ("lymphocytes", "lymphocyte", "lymphs")),
                     ("monocytes", ("monocytes", "monocyte")),
                     ("eosinophils", ("eosinophils", "eosinophil", "eos")),
                     ("basophils", ("basophils", "basophil", "baso")))
       for a in al for suf in ("absolute", "abs", "absolute_count", "count")},
    **{f"absolute_{a}": f"{k}_abs"
       for k, a in (("neutrophils", "neutrophils"), ("lymphocytes", "lymphocytes"),
                    ("monocytes", "monocytes"), ("eosinophils", "eosinophils"),
                    ("basophils", "basophils"))},
    "anc": "neutrophils_abs", "alc": "lymphocytes_abs",
}


def _canon(marker: Optional[str]) -> str:
    k = re.sub(r"[^a-z0-9]+", "_", (marker or "").strip().lower()).strip("_")
    return _ALIAS.get(k, k)


def _norm_unit(u: Optional[str]) -> str:
    """Compare units modulo formatting only — never modulo meaning.

    mg/dL vs MG/DL is the same unit; mg/dL vs g/dL is not, and must stay a conflict.
    """
    return re.sub(r"[\s.]+", "", (u or "").strip().lower())


def _num(v) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _same_num(a, b) -> bool:
    fa, fb = _num(a), _num(b)
    if fa is None and fb is None:
        return True
    if fa is None or fb is None:
        return False
    # Exact, bar float-repr wobble: 5.4 and 5.40 are the same reading, 5.4 and 5.5
    # are not. No tolerance band — a lab value that "nearly" matches is a conflict.
    return abs(fa - fb) <= 1e-9 * max(1.0, abs(fa), abs(fb))


def _index(result: Optional[dict]) -> dict[str, dict]:
    """{canonical marker -> row} for one arm's output."""
    out: dict[str, dict] = {}
    for m in (result or {}).get("markers") or []:
        if not isinstance(m, dict):
            continue
        key = _canon(m.get("marker")) or _canon(m.get("label"))
        if not key or _num(m.get("value")) is None:
            continue
        out.setdefault(key, m)      # first wins; duplicates within an arm are rare
    return out


def _row(src: dict, key: str) -> dict:
    return {
        "marker": key,
        "label": src.get("label") or src.get("marker") or key,
        "value": _num(src.get("value")),
        "unit": src.get("unit"),
        "ref_low": _num(src.get("ref_low")),
        "ref_high": _num(src.get("ref_high")),
    }


def reconcile(pdf_result: Optional[dict], text_result: Optional[dict]) -> dict:
    """Merge two independent parses into review-ready rows.

    The PDF/vision arm supplies the prefilled values wherever it ran: it is the arm
    that reads scans, and the arm that was correct on reference ranges in testing.
    The text arm's reading is never discarded though — on any disagreement it rides
    along in `alt` so the choice stays the user's.
    """
    pdf_i, text_i = _index(pdf_result), _index(text_result)
    both_ran = bool(pdf_i) and bool(text_i)

    rows, high, low = [], 0, 0
    for key in sorted(set(pdf_i) | set(text_i)):
        p, t = pdf_i.get(key), text_i.get(key)

        if not both_ran:
            # Only one arm produced anything at all — no cross-check exists. Say so
            # explicitly rather than letting it read as agreement.
            src = p or t
            rows.append({**_row(src, key), "status": "single_source",
                         "severity": "none", "diffs": [], "alt": None})
            continue

        if p is None or t is None:
            src, missing_in = (p, "text") if t is None else (t, "pdf")
            rows.append({**_row(src, key), "status": f"missing_in_{missing_in}",
                         "severity": "high", "diffs": [], "alt": None})
            high += 1
            continue

        diffs = []
        if not _same_num(p.get("value"), t.get("value")):
            diffs.append({"field": "value", "pdf": _num(p.get("value")),
                          "text": _num(t.get("value"))})
        if _norm_unit(p.get("unit")) != _norm_unit(t.get("unit")):
            diffs.append({"field": "unit", "pdf": p.get("unit"), "text": t.get("unit")})
        for f in ("ref_low", "ref_high"):
            if not _same_num(p.get(f), t.get(f)):
                diffs.append({"field": f, "pdf": _num(p.get(f)), "text": _num(t.get(f))})

        # Severity is about what a wrong answer costs. A value or unit disagreement
        # corrupts the measurement itself; a reference-range disagreement only
        # mislabels it as in/out of range, and is the known-noisy field.
        hard = any(d["field"] in ("value", "unit") for d in diffs)
        if hard:
            severity, status = "high", "conflict"
            high += 1
        elif diffs:
            severity, status = "low", "conflict"
            low += 1
        else:
            severity, status = "none", "agreed"

        rows.append({**_row(p, key), "status": status, "severity": severity,
                     "diffs": diffs,
                     "alt": _row(t, key) if diffs else None})

    pdf_date = (pdf_result or {}).get("date")
    text_date = (text_result or {}).get("date")
    date_diff = None
    if both_ran and pdf_date != text_date:
        date_diff = {"pdf": pdf_date, "text": text_date}

    return {
        "date": pdf_date or text_date,
        "date_diff": date_diff,
        "markers": rows,
        "review": {"high": high, "low": low, "total": len(rows),
                   "cross_checked": both_ran},
    }


async def parse_pdf_both_ways(pdf_bytes: bytes, text: str) -> dict:
    """Run both arms concurrently and reconcile. Raises RuntimeError if both fail.

    `text` may be empty (scanned PDF) — the text arm is then skipped rather than fed
    a few stray characters, which would manufacture a fake disagreement out of an
    arm that never really read anything.
    """
    from app.services.ai_summaries import (BLOOD_PDF_MODEL, parse_blood_report,
                                           parse_blood_report_pdf)

    MIN_TEXT_CHARS = 200
    text_usable = len(text) >= MIN_TEXT_CHARS

    tasks = [parse_blood_report_pdf(pdf_bytes)]
    if text_usable:
        tasks.append(parse_blood_report(text[:20000]))
    done = await asyncio.gather(*tasks, return_exceptions=True)

    def _ok(r):
        if isinstance(r, Exception):
            logger.error("blood parse arm failed: %s", r)
            return None
        return r if isinstance(r, dict) else None

    pdf_result = _ok(done[0])
    text_result = _ok(done[1]) if text_usable else None

    if pdf_result is None and text_result is None:
        raise RuntimeError("both parse arms failed")

    merged = reconcile(pdf_result, text_result)
    merged["sources"] = {
        "pdf": {"ok": pdf_result is not None, "model": BLOOD_PDF_MODEL,
                "markers": len(_index(pdf_result))},
        "text": {"ok": text_result is not None,
                 "model": "claude-haiku-4-5" if text_usable else None,
                 "markers": len(_index(text_result)),
                 "chars": len(text),
                 "reason": None if text_usable else
                           ("no text layer — scanned or image-only PDF"
                            if len(text) == 0 else
                            f"text layer too thin ({len(text)} chars)")},
    }
    return merged


def collapse_duplicates(result: Optional[dict]) -> tuple[Optional[dict], dict[str, list]]:
    """Fold repeated markers within ONE arm's output; return (result, conflicts).

    Overlapping screenshots show the same row twice, and a model told to report each
    analyte once still sometimes doesn't. A repeat with the same value and unit is the
    overlap — kept once, filling in a reference bound only the later copy carried. A
    repeat with a DIFFERENT value or unit is not an overlap: it is a misread or two
    distinct analytes the canonicaliser merged, and `_index`'s first-wins would pick
    one silently. Those come back in `conflicts` {key: [readings]} for the user.
    """
    if not result:
        return result, {}
    kept: dict[str, dict] = {}
    order: list[str] = []
    conflicts: dict[str, list] = {}
    for m in result.get("markers") or []:
        if not isinstance(m, dict):
            continue
        key = _canon(m.get("marker")) or _canon(m.get("label"))
        if not key or _num(m.get("value")) is None:
            continue
        first = kept.get(key)
        if first is None:
            kept[key] = dict(m)
            order.append(key)
            continue
        if _same_num(first.get("value"), m.get("value")) \
                and _norm_unit(first.get("unit")) == _norm_unit(m.get("unit")):
            for f in ("ref_low", "ref_high"):
                if _num(first.get(f)) is None and _num(m.get(f)) is not None:
                    first[f] = m.get(f)
            continue
        readings = conflicts.setdefault(key, [_reading(first)])
        readings.append(_reading(m))
    return {**result, "markers": [kept[k] for k in order]}, conflicts


def _reading(m: dict) -> dict:
    return {"value": _num(m.get("value")), "unit": m.get("unit"), "image": _image_no(m)}


def _image_no(m: Optional[dict]) -> Optional[int]:
    try:
        return int((m or {}).get("image"))
    except (TypeError, ValueError):
        return None


async def parse_images_both_ways(images: list[tuple[bytes, str]]) -> dict:
    """Two models read the same screenshots; reconcile. Raises RuntimeError if both fail.

    Same output shape as `parse_pdf_both_ways`, so the review UI and save path are
    shared. The arms still sit in the `pdf` / `text` slots `reconcile` uses (primary /
    check); `sources.arms` names what each slot really was, and `sources.kind` is
    "images" so the UI never calls a two-model check a vision-vs-text one.
    """
    from app.services.ai_summaries import (BLOOD_IMAGE_CHECK_MODEL, BLOOD_PDF_MODEL,
                                           parse_blood_report_images)

    done = await asyncio.gather(
        parse_blood_report_images(images, BLOOD_PDF_MODEL, "blood_parse_images"),
        parse_blood_report_images(images, BLOOD_IMAGE_CHECK_MODEL, "blood_parse_images_check"),
        return_exceptions=True)

    def _ok(r):
        if isinstance(r, Exception):
            logger.error("blood image parse arm failed: %s", r)
            return None
        return r if isinstance(r, dict) else None

    primary, primary_dups = collapse_duplicates(_ok(done[0]))
    check, check_dups = collapse_duplicates(_ok(done[1]))
    if primary is None and check is None:
        raise RuntimeError("both parse arms failed")

    merged = reconcile(primary, check)
    p_i, c_i = _index(primary), _index(check)
    n = len(images)
    for row in merged["markers"]:
        key = row["marker"]
        img = _image_no(p_i.get(key)) or _image_no(c_i.get(key))
        row["image"] = img if img is not None and 1 <= img <= n else None
        dups = primary_dups.get(key) or check_dups.get(key)
        if dups:
            # A repeat that disagrees with itself needs the user's eyes whatever the
            # other arm said — escalate it, counted once.
            if row["severity"] != "high":
                merged["review"]["high"] += 1
                if row["severity"] == "low":
                    merged["review"]["low"] -= 1
            row.update(status="duplicate_in_images", severity="high", dups=dups)

    merged["sources"] = {
        "kind": "images",
        "images": n,
        "arms": {"pdf": "Opus", "text": "Sonnet"},
        "pdf": {"ok": primary is not None, "model": BLOOD_PDF_MODEL,
                "markers": len(p_i), "duplicates": len(primary_dups)},
        "text": {"ok": check is not None, "model": BLOOD_IMAGE_CHECK_MODEL,
                 "markers": len(c_i), "duplicates": len(check_dups),
                 # The banner reads `text.reason` for "why only one parse" — name the
                 # arm that actually failed, whichever slot it sat in.
                 "reason": ("the Opus parse failed" if primary is None else
                            "the Sonnet check failed" if check is None else None)},
    }
    return merged
