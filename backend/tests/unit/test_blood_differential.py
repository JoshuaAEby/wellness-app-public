"""White-cell differential (2026-10-28): absolute counts carry the range judgement; the
percentages are shown but never flagged. Values are a synthetic CBC with the same shape:
low WBC, a slightly low neutrophil % and a high lymphocyte %, all absolute counts in range."""
import asyncio
from datetime import date

from app.services import blood_markers as bm
from app.services.blood_reconcile import _canon

D = "2026-10-26"
CBC = [  # (marker, value, unit, ref_low, ref_high)
    ("wbc", 4.6, "10³/µL", 5.0, 10.0),
    ("neutrophils", 44.0, "%", 45.0, 76.0),
    ("lymphocytes", 47.0, "%", 15.0, 45.0),
    ("monocytes", 7.0, "%", 4.0, 13.0),
    ("eosinophils", 1.0, "%", 0.0, 7.0),
    ("basophils", 1.0, "%", 0.0, 2.0),
]


def _rows(spec=CBC, d=D):
    return [{"id": k, "date": d, "marker": m, "label": m, "value": v, "unit": u,
             "ref_low": lo, "ref_high": hi} for k, (m, v, u, lo, hi) in enumerate(spec)]


def test_derives_absolute_counts_from_same_draw():
    out = {r["marker"]: r for r in bm.with_absolute_counts(_rows())}
    assert out["neutrophils_abs"]["value"] == 2.02          # 4.6 × 44.0%
    assert out["lymphocytes_abs"]["value"] == 2.16
    assert out["monocytes_abs"]["value"] == 0.32
    assert out["neutrophils_abs"]["derived"] is True and out["neutrophils_abs"]["id"] is None
    for k in ("neutrophils_abs", "lymphocytes_abs", "monocytes_abs",
              "eosinophils_abs", "basophils_abs"):
        r = out[k]
        assert not bm.assess(k, r["value"], r["ref_low"], r["ref_high"])["concern"], k


def test_no_derivation_without_wbc_or_percent_or_when_lab_reports_it():
    assert len(bm.with_absolute_counts(_rows(CBC[1:]))) == 5          # no WBC
    non_pct = [CBC[0], ("neutrophils", 1.9, "x10E3/uL", 1.4, 7.0)]   # lab's absolute
    assert len(bm.with_absolute_counts(_rows(non_pct))) == 2
    lab_abs = CBC[:2] + [("neutrophils_abs", 1.9, "x10E3/uL", 1.4, 7.0)]
    assert [r["value"] for r in bm.with_absolute_counts(_rows(lab_abs))
            if r["marker"] == "neutrophils_abs"] == [1.9]           # the lab's, not ours
    other_day = _rows(CBC[:1], "2025-09-08") + _rows(CBC[1:2])
    assert len(bm.with_absolute_counts(other_day)) == 2              # WBC must be same draw


def test_wbc_in_cells_per_ul_is_rescaled():
    rows = _rows([("wbc", 4600, "/uL", 5000, 10000), CBC[1]])
    abs_n = [r for r in bm.with_absolute_counts(rows) if r["marker"] == "neutrophils_abs"]
    assert abs_n[0]["value"] == 2.02


def test_percent_rows_defer_to_absolute():
    keys = {r["marker"] for r in bm.with_absolute_counts(_rows())}
    a = bm.assess_in_panel("lymphocytes", 47.0, "%", 15.0, 45.0, keys)
    assert a["position"] == "above" and a["concern"] is False
    assert a["read_as"] == "lymphocytes_abs" and a["severity"] is None
    # Without an absolute count in the draw, the % row is judged as before.
    b = bm.assess_in_panel("lymphocytes", 47.0, "%", 15.0, 45.0, {"lymphocytes"})
    assert b["concern"] is True and b["read_as"] is None


def test_low_eosinophils_and_basophils_are_unremarkable():
    assert not bm.assess("eosinophils", 0.0, 0.5, 7.0)["concern"]
    assert not bm.near_bound("eosinophils", 0.7, 0.0, 7.0)
    assert bm.assess("eosinophils", 9.0, 0.0, 7.0)["concern"]          # high still counts


def test_parser_aliases_for_absolute_counts():
    assert _canon("Neutrophils (Absolute)") == "neutrophils_abs"
    assert _canon("Lymphs (Absolute)") == "lymphocytes_abs"
    assert _canon("ANC") == "neutrophils_abs"
    assert _canon("Neutrophils") == "neutrophils"                    # % row untouched


def _seed():
    from app.database import AsyncSessionLocal
    from app.models import BloodMarker

    async def go():
        async with AsyncSessionLocal() as s:
            from sqlalchemy import delete
            await s.execute(delete(BloodMarker))
            for m, v, u, lo, hi in CBC:
                s.add(BloodMarker(date=date(2026, 10, 26), marker=m, label=m.title(),
                                  value=v, unit=u, ref_low=lo, ref_high=hi))
            await s.commit()
    asyncio.run(go())


def test_list_endpoint_flags_only_wbc(db_schema):
    _seed()
    from app.database import AsyncSessionLocal
    from app.routers.blood import list_markers

    async def go():
        async with AsyncSessionLocal() as s:
            return await list_markers(db=s, _="owner")
    rows = {r["marker"]: r for r in asyncio.run(go())}
    assert {k for k, r in rows.items() if r["concern"]} == {"wbc"}
    assert rows["lymphocytes"]["read_as"] == "lymphocytes_abs"
    assert rows["lymphocytes"]["near_bound"] is False
    assert rows["neutrophils_abs"]["derived"] is True
    assert rows["neutrophils_abs"]["group"] == "Blood count"
    assert rows["neutrophils_abs"]["rcv_pct"] is not None


def test_briefing_block_matches_the_card(db_schema):
    _seed()
    from app.database import AsyncSessionLocal
    from app.services.summary_context import blood_block

    async def go():
        async with AsyncSessionLocal() as s:
            return await blood_block(s)
    text = asyncio.run(go())
    lines = {ln.strip().split(":")[0]: ln for ln in text.splitlines() if ":" in ln}
    assert "⚠" in lines["Wbc"]
    assert "⚠" not in lines["Lymphocytes"] and "judge by the absolute" in lines["Lymphocytes"]
    assert "calculated WBC × %" in lines["Neut (abs)"]
    assert "⚠" not in lines["Neut (abs)"]
