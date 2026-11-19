"""Blood-marker tracking: paste-parse (LLM) or manual entry, 1–2 panels/year.

These are the slow ground-truth outcomes the wearable metrics proxy for; panels
pair naturally with the interventions/ITS machinery ("started creatine in March
→ ApoB delta at the next draw"). Flat rows keyed (date, marker) — upserts make
re-pasting the same report idempotent.
"""
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.models import BloodMarker, BloodMarkerTarget

router = APIRouter(prefix="/api/blood", tags=["blood"])


class ParseIn(BaseModel):
    text: str


class MarkerIn(BaseModel):
    marker: str
    label: Optional[str] = None
    value: float
    unit: Optional[str] = None
    ref_low: Optional[float] = None
    ref_high: Optional[float] = None


class PanelIn(BaseModel):
    date: date
    markers: list[MarkerIn]


@router.post("/parse")
async def parse_report(
    body: ParseIn,
    _: str = Depends(get_current_user),
):
    """LLM-extract markers from pasted lab text (same pattern as food parsing).
    Returns {date|null, markers:[...]} for user confirmation — does NOT save."""
    from app.services.ai_summaries import parse_blood_report
    result = await parse_blood_report(body.text)
    if result is None:
        raise HTTPException(status_code=503,
                            detail="AI parsing unavailable — check API key / AI setting")
    return result


# Lab PDFs are typically a few hundred KB; anything far beyond that is not a lab
# report and shouldn't be read into memory. Also well under the API's 32 MB request
# ceiling once base64 inflates it ~4/3.
MAX_PDF_BYTES = 15 * 1024 * 1024
# The Anthropic API renders every page of a `document` block, and caps at 600 pages
# (100 on 200k-context models). A lab panel is single digits; a hundred-page PDF is
# not a lab report, and refusing early beats a slow, expensive API rejection.
MAX_PDF_PAGES = 60
# How much extracted text to echo back for user verification.
TEXT_PREVIEW_CHARS = 4000


def _read_pdf(data: bytes) -> tuple[str, bytes]:
    """Return (text layer, bytes safe to hand the vision arm).

    Raises HTTPException on unreadable input. The returned bytes differ from the
    input only for encrypted PDFs: pypdf can read those after a blank-password
    decrypt, but the *original* bytes are still encrypted and the API can't open
    them — so a decrypted copy is re-serialised, or the two arms would disagree for
    a reason that has nothing to do with the report.
    """
    import io
    from pypdf import PdfReader, PdfWriter
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Could not read PDF: {exc}")

    out_bytes = data
    if reader.is_encrypted:
        # A blank user-password is common on lab PDFs and often decrypts fine.
        try:
            reader.decrypt("")
            writer = PdfWriter()
            for page in reader.pages:
                writer.add_page(page)
            buf = io.BytesIO()
            writer.write(buf)
            out_bytes = buf.getvalue()
        except Exception:
            raise HTTPException(status_code=422,
                                detail="PDF is password-protected — remove the password and retry")

    if len(reader.pages) > MAX_PDF_PAGES:
        raise HTTPException(
            status_code=422,
            detail=f"PDF has {len(reader.pages)} pages (max {MAX_PDF_PAGES}) — "
                   "upload just the results pages.")

    parts = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            continue          # one unreadable page shouldn't lose the rest
    return "\n".join(parts).strip(), out_bytes


@router.post("/parse-pdf")
async def parse_report_pdf(
    file: UploadFile = File(...),
    _: str = Depends(get_current_user),
):
    """Parse the PDF TWICE — vision and text — and return reconciled rows.

    A panel is entered once and trended for years, so a silent misparse is permanent
    and undetectable downstream. Two independent arms (different model AND different
    modality) cross-check each other; disagreements come back flagged for the user to
    resolve rather than being silently resolved here. See services/blood_reconcile.

    A scanned PDF is no longer rejected — the vision arm reads it. It just comes back
    marked `single_source`, because with no text layer there is nothing to check it
    against.
    """
    if file.content_type not in ("application/pdf", "application/octet-stream") \
            and not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=422, detail="Not a PDF")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="Empty file")
    if len(data) > MAX_PDF_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"PDF too large ({len(data)//1024} KB, max {MAX_PDF_BYTES//1024} KB)")

    text, pdf_bytes = _read_pdf(data)

    from app.services.blood_reconcile import parse_pdf_both_ways
    try:
        result = await parse_pdf_both_ways(pdf_bytes, text)
    except RuntimeError:
        raise HTTPException(status_code=503,
                            detail="AI parsing unavailable — check API key / AI setting")

    # Echo the extracted text so the user can SEE what one arm actually read. Lab PDFs
    # are often absolutely-positioned cards (value in a callout bubble, axis ticks as
    # separate runs), so extraction order interleaves — a stray tick number sitting
    # next to the real value is exactly how a silent misparse happens. Showing the
    # source makes "is this the parser or the PDF?" answerable instead of a guess.
    result["source_chars"] = len(text)
    result["filename"] = file.filename
    result["text_preview"] = text[:TEXT_PREVIEW_CHARS]
    result["text_truncated"] = len(text) > TEXT_PREVIEW_CHARS
    return result


# Screenshot panels (2026-10-28). The client downscales to ≤1568 px on the long edge
# (the API's own resize ceiling) and re-encodes as JPEG, so a screenshot arrives at a
# few hundred KB; these are guards, not expected sizes. Per image: base64 inflates 4/3,
# and 3.5 MB → 4.7 MB stays under the API's 5 MB image limit. Total: the request is
# sent twice (two arms) and each must stay under the API's 32 MB body after base64.
MAX_BLOOD_IMAGES = 20
MAX_BLOOD_IMAGE_BYTES = 3_500_000
MAX_BLOOD_IMAGES_TOTAL = 20_000_000


def _sniff_image(data: bytes) -> Optional[str]:
    """Media type from the magic bytes — the declared content type is not trusted (iOS
    sends octet-stream for some picks), and the API rejects a mislabelled image."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


def _is_heif(data: bytes) -> bool:
    return data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"mif1", b"msf1",
                                                   b"heim", b"heis", b"hevc", b"avif")


@router.post("/parse-images")
async def parse_report_images(
    files: list[UploadFile] = File(...),
    _: str = Depends(get_current_user),
):
    """Parse several screenshots of ONE report, in the order given, as one panel.

    Same contract as /parse-pdf (reconciled rows, nothing saved) so the review and save
    steps are shared. Both arms are vision — see services/blood_reconcile
    `parse_images_both_ways` for what that cross-check can and cannot catch.
    """
    if len(files) > MAX_BLOOD_IMAGES:
        raise HTTPException(status_code=413,
                            detail=f"{len(files)} images — max {MAX_BLOOD_IMAGES} per panel")
    images: list[tuple[bytes, str]] = []
    total = 0
    for k, f in enumerate(files, start=1):
        data = await f.read()
        name = f.filename or f"image {k}"
        if not data:
            raise HTTPException(status_code=422, detail=f"{name} is empty")
        media = _sniff_image(data)
        if media is None:
            raise HTTPException(
                status_code=422,
                detail=(f"{name} is HEIC, which the parser can't take — this browser "
                        "couldn't convert it; export it as JPEG/PNG and retry")
                if _is_heif(data) else f"{name} is not a PNG/JPEG/WebP image")
        if len(data) > MAX_BLOOD_IMAGE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"{name} is {len(data) // 1024} KB (max {MAX_BLOOD_IMAGE_BYTES // 1024} KB)")
        total += len(data)
        images.append((data, media))
    if not images:
        raise HTTPException(status_code=422, detail="No images")
    if total > MAX_BLOOD_IMAGES_TOTAL:
        raise HTTPException(
            status_code=413,
            detail=f"Images total {total // 1024} KB (max {MAX_BLOOD_IMAGES_TOTAL // 1024} KB)")

    from app.services.blood_reconcile import parse_images_both_ways
    try:
        result = await parse_images_both_ways(images)
    except RuntimeError:
        raise HTTPException(status_code=503,
                            detail="AI parsing unavailable — check API key / AI setting")
    result["filenames"] = [f.filename for f in files]
    return result


@router.get("")
async def list_markers(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """All marker rows, newest draw first (frontend groups by marker/date).

    Each row carries its direction-aware assessment rather than leaving the client to
    re-derive `value > ref_high`. The card and the summary context previously each
    made that call independently and both got HDL wrong; serving it from one place is
    what stops them drifting again.
    """
    from app.services.blood_markers import (assess_in_panel, default_target, group,
                                            near_bound, rcv_pct, vs_target,
                                            with_absolute_counts)

    rows = (await db.execute(
        select(BloodMarker).order_by(BloodMarker.date.desc(), BloodMarker.marker)
    )).scalars().all()
    overrides = {t.marker: t for t in (await db.execute(
        select(BloodMarkerTarget)
    )).scalars().all()}

    def target_for(marker: str):
        """User override wins; otherwise the seeded default. Returns
        (low, high, source, rationale) — `source` lets the UI say whose number it is."""
        o = overrides.get(marker)
        if o is not None:
            # A row with both bounds NULL is an explicit "no target", not a miss.
            return o.target_low, o.target_high, "custom", None
        d = default_target(marker)
        if d is None:
            return None, None, None, None
        return d[0], d[1], "default", d[2]

    # Absolute differential counts are derived per draw (WBC × %) and never stored —
    # see blood_markers "White-cell differential". Derived rows sort with their draw.
    base = [{"id": r.id, "date": r.date.isoformat(), "marker": r.marker, "label": r.label,
             "value": r.value, "unit": r.unit, "ref_low": r.ref_low, "ref_high": r.ref_high,
             "derived": False} for r in rows]
    full = with_absolute_counts(base)
    full.sort(key=lambda r: (r["date"], r["marker"]))
    full.sort(key=lambda r: r["date"], reverse=True)
    panel_markers: dict[str, set] = {}
    for r in full:
        panel_markers.setdefault(r["date"], set()).add(r["marker"])

    out = []
    for r in full:
        t_low, t_high, t_src, t_why = target_for(r["marker"])
        a = assess_in_panel(r["marker"], r["value"], r["unit"], r["ref_low"], r["ref_high"],
                            panel_markers[r["date"]])
        out.append({
            **r,
            # % a repeat draw must exceed to be distinguishable from biological +
            # analytical noise. Null when the marker has no published CV — the card
            # must then show the delta WITHOUT a significance claim.
            "rcv_pct": rcv_pct(r["marker"]),
            # Panel the marker belongs to, and whether it's in range but hugging the
            # edge that matters. Both live server-side so the card's triage rules and
            # the summary context can't drift apart.
            "group": group(r["marker"]),
            # A % row that defers to its absolute count is never "near" anything either.
            "near_bound": (not a["read_as"]) and near_bound(
                r["marker"], r["value"], r["ref_low"], r["ref_high"]),
            # Your goal, kept strictly separate from the lab's population interval.
            # `vs_target` is NOT folded into `concern` — being off an editable target
            # is a weaker, different claim than being outside the lab's range.
            "target_low": t_low, "target_high": t_high,
            "target_source": t_src, "target_rationale": t_why,
            "vs_target": vs_target(r["value"], t_low, t_high),
            **a,
        })
    return out


class TargetIn(BaseModel):
    target_low: Optional[float] = None
    target_high: Optional[float] = None


@router.put("/targets/{marker}")
async def set_target(
    marker: str,
    body: TargetIn,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Pin a personal target. Both bounds null is a valid state — an explicit
    "no target here", which is how a seeded default gets switched off."""
    key = marker.strip().lower().replace(" ", "_")
    if (body.target_low is not None and body.target_high is not None
            and body.target_low >= body.target_high):
        raise HTTPException(status_code=400, detail="target_low must be below target_high")
    stmt = sqlite_insert(BloodMarkerTarget).values(
        marker=key, target_low=body.target_low, target_high=body.target_high,
        updated_at=datetime.utcnow(),
    ).on_conflict_do_update(
        index_elements=["marker"],
        set_={"target_low": body.target_low, "target_high": body.target_high,
              "updated_at": datetime.utcnow()},
    )
    await db.execute(stmt)
    await db.commit()
    return {"marker": key, "target_low": body.target_low, "target_high": body.target_high,
            "target_source": "custom"}


@router.delete("/targets/{marker}")
async def reset_target(
    marker: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Drop the override and fall back to the seeded default."""
    from app.services.blood_markers import default_target
    key = marker.strip().lower().replace(" ", "_")
    row = (await db.execute(
        select(BloodMarkerTarget).where(BloodMarkerTarget.marker == key)
    )).scalar_one_or_none()
    if row is not None:
        await db.delete(row)
        await db.commit()
    d = default_target(key)
    return {"marker": key, "target_low": d[0] if d else None,
            "target_high": d[1] if d else None,
            "target_source": "default" if d else None}


@router.post("")
async def save_panel(
    body: PanelIn,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    """Bulk-upsert one draw date's markers (confirmed parse or manual rows)."""
    if not body.markers:
        raise HTTPException(status_code=400, detail="no markers provided")
    from app.services.blood_markers import normalize_unit

    for m in body.markers:
        key = m.marker.strip().lower().replace(" ", "_")
        # Repair PDF-extraction damage on the way in — superscript escapes
        # ("10\S\3/uL"), empty unit cells, and non-units like "ratio" landing on a
        # concentration marker. Narrow by design: a clean unit is never converted.
        unit = normalize_unit(key, m.unit)
        stmt = sqlite_insert(BloodMarker).values(
            date=body.date, marker=key, label=m.label, value=m.value,
            unit=unit, ref_low=m.ref_low, ref_high=m.ref_high,
        ).on_conflict_do_update(
            index_elements=["date", "marker"],
            set_={"label": m.label, "value": m.value, "unit": unit,
                  "ref_low": m.ref_low, "ref_high": m.ref_high},
        )
        await db.execute(stmt)
    await db.commit()
    return {"saved": len(body.markers), "date": body.date.isoformat()}


@router.delete("/{marker_id}")
async def delete_marker(
    marker_id: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
):
    row = (await db.execute(
        select(BloodMarker).where(BloodMarker.id == marker_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    await db.delete(row)
    await db.commit()
    return {"deleted": marker_id}
