"""Blood panel from screenshots (2026-10-28): several images → ONE reconciled panel.

No API calls: the model arms are replaced with canned outputs shaped like real ones.
"""
import asyncio
import io

import httpx
import pytest
from fastapi import FastAPI

from app.services import ai_summaries, blood_reconcile
from app.services.blood_reconcile import collapse_duplicates

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
JPG = b"\xff\xd8\xff\xe0" + b"\0" * 32
HEIC = b"\0\0\0\x18ftypheic" + b"\0" * 32


def _m(marker, value, unit="mg/dL", image=None, lo=None, hi=None):
    return {"marker": marker, "label": marker.upper(), "value": value, "unit": unit,
            "ref_low": lo, "ref_high": hi, "image": image}


def test_overlap_duplicate_collapses_and_keeps_late_ref():
    res, conflicts = collapse_duplicates({"date": "2026-10-01", "markers": [
        _m("ldl_c", 92, image=1), _m("hdl_c", 61, image=1),
        _m("LDL cholesterol", 92.0, "mg/dl", image=2, hi=100),   # overlap row, alias + case
    ]})
    assert conflicts == {}
    keys = [m["marker"] for m in res["markers"]]
    assert keys == ["ldl_c", "hdl_c"]
    assert res["markers"][0]["ref_high"] == 100                  # filled from the repeat


def test_vldl_names_fold_to_the_stored_key():
    # Live 2026-10-28: Opus said vldl_c, Sonnet vldl_cholesterol_cal — one marker.
    out = blood_reconcile.reconcile({"markers": [_m("vldl_c", 18)]},
                                    {"markers": [_m("VLDL Cholesterol Cal", 18)]})
    assert [(r["marker"], r["status"]) for r in out["markers"]] == [("vldl_c", "agreed")]


def test_disagreeing_duplicate_is_a_conflict_not_first_wins():
    res, conflicts = collapse_duplicates({"markers": [
        _m("glucose", 88, image=1), _m("fasting_glucose", 98, image=2)]})
    assert len(res["markers"]) == 1
    assert conflicts["fasting_glucose"] == [
        {"value": 88.0, "unit": "mg/dL", "image": 1},
        {"value": 98.0, "unit": "mg/dL", "image": 2}]


def _fake_arms(monkeypatch, primary, check):
    calls = []

    async def fake(images, model, feature):
        calls.append((len(images), model, feature))
        out = primary if model == ai_summaries.BLOOD_PDF_MODEL else check
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(ai_summaries, "parse_blood_report_images", fake)
    return calls


def test_two_arms_one_panel(monkeypatch):
    # A table spanning two screenshots with one overlapping row; the date only on image 1.
    primary = {"date": "2026-10-01", "markers": [
        _m("apob", 88, image=1), _m("ldl_c", 92, image=1),
        _m("ldl_c", 92, image=2), _m("hdl_c", 61, image=2),
        _m("tsh", 2.1, "uIU/mL", image=2), _m("tsh", 2.7, "uIU/mL", image=3)]}
    check = {"date": "2026-10-01", "markers": [
        _m("apob", 88, image=1), _m("ldl_c", 92, image=2),
        _m("hdl_c", 16, image=2), _m("tsh", 2.1, "uIU/mL", image=2)]}
    calls = _fake_arms(monkeypatch, primary, check)
    out = asyncio.run(blood_reconcile.parse_images_both_ways([(PNG, "image/png")] * 3))

    assert sorted(c[2] for c in calls) == ["blood_parse_images", "blood_parse_images_check"]
    assert all(c[0] == 3 for c in calls)                      # every image, one request each
    rows = {r["marker"]: r for r in out["markers"]}
    assert sorted(rows) == ["apob", "hdl_c", "ldl_c", "tsh"]  # no duplicate markers
    assert out["date"] == "2026-10-01" and out["date_diff"] is None
    assert rows["apob"]["status"] == "agreed" and rows["apob"]["image"] == 1
    assert rows["ldl_c"]["status"] == "agreed" and rows["ldl_c"]["image"] == 1
    assert rows["hdl_c"]["severity"] == "high"                 # 61 vs 16
    assert rows["hdl_c"]["diffs"][0] == {"field": "value", "pdf": 61.0, "text": 16.0}
    # Primary saw TSH twice with different values; the check model saw it once and
    # agreed with the first. Still escalated — the repeat disagreed with itself.
    assert rows["tsh"]["status"] == "duplicate_in_images"
    assert [d["value"] for d in rows["tsh"]["dups"]] == [2.1, 2.7]
    assert out["review"] == {"high": 2, "low": 0, "total": 4, "cross_checked": True}
    assert out["sources"]["kind"] == "images" and out["sources"]["images"] == 3
    assert out["sources"]["pdf"]["duplicates"] == 1


def test_one_arm_failing_is_single_source(monkeypatch):
    _fake_arms(monkeypatch, {"date": None, "markers": [_m("apob", 88, image=9)]},
               RuntimeError("boom"))
    out = asyncio.run(blood_reconcile.parse_images_both_ways([(JPG, "image/jpeg")]))
    assert out["review"]["cross_checked"] is False
    assert out["markers"][0]["status"] == "single_source"
    assert out["markers"][0]["image"] is None                 # 9 of 1 is out of range
    assert out["sources"]["text"]["reason"] == "the Sonnet check failed"


def test_both_arms_failing_raises(monkeypatch):
    _fake_arms(monkeypatch, None, None)
    with pytest.raises(RuntimeError):
        asyncio.run(blood_reconcile.parse_images_both_ways([(PNG, "image/png")]))


def test_request_keeps_order_and_captions(monkeypatch):
    seen = {}

    class Resp:
        content = [type("B", (), {"type": "text", "text": '```json\n{"date": null, "markers": []}\n```'})()]
        usage = type("U", (), {"input_tokens": 10, "output_tokens": 5})()
        stop_reason = "end_turn"

    async def fake_create(client, kwargs, feature):
        seen.update(kwargs=kwargs, feature=feature)
        return Resp()

    async def fake_log(*a, **k):
        seen["log"] = (a, k)

    from app.config import settings
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(ai_summaries, "_create_complete", fake_create)
    monkeypatch.setattr(ai_summaries, "_log_call", fake_log)
    out = asyncio.run(ai_summaries.parse_blood_report_images(
        [(PNG, "image/png"), (JPG, "image/jpeg")], "claude-opus-5", "blood_parse_images"))
    assert out == {"date": None, "markers": []}
    content = seen["kwargs"]["messages"][0]["content"]
    assert [c["type"] for c in content] == ["text", "image", "text", "image", "text"]
    assert content[0]["text"] == "Image 1 of 2:" and content[2]["text"] == "Image 2 of 2:"
    assert [content[1]["source"]["media_type"], content[3]["source"]["media_type"]] == \
        ["image/png", "image/jpeg"]
    assert "2 images" in content[4]["text"]
    assert "temperature" not in seen["kwargs"]                 # Claude 5 rejects it
    assert seen["log"][1]["model"] == "claude-opus-5"           # priced as Opus, not Haiku


def _client(monkeypatch, result=None):
    from app.auth import get_current_user
    from app.routers import blood

    got = {}

    async def fake(images):
        got["images"] = images
        return result or {"date": None, "markers": [], "review": {}, "sources": {}}

    monkeypatch.setattr(blood_reconcile, "parse_images_both_ways", fake)
    app = FastAPI()
    app.include_router(blood.router)
    app.dependency_overrides[get_current_user] = lambda: "owner"
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t"), got


def _post(monkeypatch, files):
    async def go():
        client, got = _client(monkeypatch)
        async with client:
            r = await client.post("/api/blood/parse-images", files=[
                ("files", (name, io.BytesIO(data), ctype)) for name, data, ctype in files])
        return r, got
    return asyncio.run(go())


def test_endpoint_sniffs_types_and_keeps_order(monkeypatch):
    r, got = _post(monkeypatch, [("b.png", PNG, "application/octet-stream"),
                                 ("a.jpg", JPG, "image/jpeg")])
    assert r.status_code == 200
    assert [m for _, m in got["images"]] == ["image/png", "image/jpeg"]
    assert r.json()["filenames"] == ["b.png", "a.jpg"]


def test_endpoint_rejects_heic_with_a_reason(monkeypatch):
    r, _ = _post(monkeypatch, [("IMG_1.HEIC", HEIC, "image/heic")])
    assert r.status_code == 422 and "HEIC" in r.json()["detail"]


def test_endpoint_rejects_non_images_and_limits(monkeypatch):
    from app.routers import blood
    r, _ = _post(monkeypatch, [("x.pdf", b"%PDF-1.7", "application/pdf")])
    assert r.status_code == 422
    r, _ = _post(monkeypatch, [(f"{k}.png", PNG, "image/png")
                               for k in range(blood.MAX_BLOOD_IMAGES + 1)])
    assert r.status_code == 413
    big = PNG + b"\0" * blood.MAX_BLOOD_IMAGE_BYTES
    r, _ = _post(monkeypatch, [("big.png", big, "image/png")])
    assert r.status_code == 413 and "big.png" in r.json()["detail"]
