#!/usr/bin/env python3
"""
Screen Time collector for the wellness-app.

Runs on the Mac (launchd, every 30 min). Reads local app usage and — if Screen
Time "Share Across Devices" is on — iPhone app usage that has synced to this
Mac, then POSTs new segments to the wellness server.

  Mac   : ~/Library/Application Support/Knowledge/knowledgeC.db
          table ZOBJECT, ZSTREAMNAME = '/app/usage'
          (Cocoa/Mac-absolute timestamps; + 978307200 -> Unix)
  iPhone: ~/Library/Biome/streams/(public|restricted)/app.InFocus/**
          SEGB1/SEGB2 files (parsed by the vendored ccl_segb package)

Deduplication: a per-source watermark is kept in `.screentime_last` next to this
script so each run only sends *new* segments. The server endpoint is also
idempotent (UNIQUE(app, start_time, device)), so re-sending is harmless.

Design constraints:
  - Runs on the stock macOS `/usr/bin/python3` (currently 3.9): standard library
    only, plus the vendored SEGB parser under ./vendor. No pip installs.
  - Full Disk Access is REQUIRED to read knowledgeC.db and the Biome streams —
    grant it to the interpreter that runs this script (see the .plist header).

Configuration (environment variables):
  WELLNESS_URL          Base URL of the server        (default: http://localhost:8093)
  WELLNESS_WIDGET_TOKEN Bearer token (same WIDGET_TOKEN as backend/.env)  [REQUIRED]
  WELLNESS_CA_BUNDLE    Path to a CA cert for TLS verification
                        (default: ./ca.crt if present, else system trust)
  WELLNESS_VERIFY_TLS   "0" to disable TLS verification entirely (default "1")
  TZ                    Local timezone for emitted ISO offsets (e.g. America/New_York)

CLI flags:
  --dry-run       Collect and print a summary, but do NOT POST or advance state.
  --debug         Verbose per-record diagnostics (esp. for the iPhone/SEGB path).
  --mac-only      Collect only the Mac (knowledgeC) source.
  --iphone-only   Collect only the iPhone (Biome) source.
  --since DAYS    On this run, look back DAYS days instead of using the watermark.
  --first-days N  Look-back window used only when no watermark exists yet (default 7).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import ssl
import sqlite3
import struct
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── make the vendored SEGB parser importable ────────────────────────────────
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE / "vendor"))
try:
    from ccl_segb import (  # noqa: E402
        read_segb_file, EntryState,
        file_matches_segbv1_signature, file_matches_segbv2_signature,
    )
    _SEGB_AVAILABLE = True
except Exception as _segb_exc:                        # pragma: no cover
    _SEGB_AVAILABLE = False
    _SEGB_IMPORT_ERROR = _segb_exc

# ── constants ───────────────────────────────────────────────────────────────
COCOA_OFFSET = 978307200                              # seconds between 1970 and 2001 epochs
STATE_FILE = _HERE / ".screentime_last"
DEFAULT_URL = "http://localhost:8093"
KNOWLEDGE_DB = Path.home() / "Library/Application Support/Knowledge/knowledgeC.db"
BIOME_DIRS = [
    Path.home() / "Library/Biome/streams/public/app.InFocus",
    Path.home() / "Library/Biome/streams/restricted/app.InFocus",
    Path.home() / "Library/Biome/streams/app.InFocus",   # spec-listed fallback path
]
POST_CHUNK = 1000                                     # records per POST request


# ── small utilities ─────────────────────────────────────────────────────────
# Per-source problems found this run, reported to the server (POST /api/screentime/status,
# note 79) so its alerts can name the cause — on 2026-10-22 a macOS update revoked Full
# Disk Access and the only record was this log file. source -> {"code", "detail"}.
PROBLEMS = {}


def problem(source: str, code: str, detail: str) -> None:
    PROBLEMS[source] = {"code": code, "detail": detail[:300]}


def log(msg: str) -> None:
    # Timestamped because launchd appends every run to one file with no run
    # separator. Without this you cannot tell a stale log from a busy one, and
    # you cannot measure how far behind a source is running.
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {msg}", flush=True)


def dbg(enabled: bool, msg: str) -> None:
    if enabled:
        print(msg, flush=True)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            log(f"WARN: could not parse {STATE_FILE}; treating as empty")
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _fmt_lag(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def unix_to_local_iso(unix_ts: float, tz: timezone) -> str:
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).astimezone(tz).isoformat()


def local_tz() -> timezone:
    # Honour TZ via the OS; astimezone() with no arg uses the system local zone.
    return datetime.now().astimezone().tzinfo  # type: ignore[return-value]


# ── Mac source: knowledgeC.db /app/usage ────────────────────────────────────
def _snapshot_knowledge_db(tmpdir: str) -> Path:
    """Copy knowledgeC.db together with its -wal/-shm sidecars, return the copy.

    knowledgeC is a LIVE WAL-mode database: macOS buffers recent activity in
    knowledgeC.db-wal and only folds it into the main file at a checkpoint,
    which can be many hours apart. Opening the original with `immutable=1`
    (as this did until 2026-07-19) tells SQLite the file can never change, so
    SQLite skips the -wal entirely and we silently saw only checkpointed data
    — hours-to-days stale, and erratic because capture tracked checkpoint
    timing rather than actual usage.

    Copying all three files and reading the COPY lets SQLite replay the WAL,
    so we see everything up to the moment. We never touch the live DB (the
    copy is opened read-write purely so WAL recovery can run on it).

    A torn tail is self-healing: if macOS writes mid-copy, WAL recovery stops
    at the last valid frame and the next run picks up the remainder.
    """
    dest = Path(tmpdir) / "knowledgeC.db"
    shutil.copyfile(KNOWLEDGE_DB, dest)
    for suffix in ("-wal", "-shm"):
        side = Path(str(KNOWLEDGE_DB) + suffix)
        if side.exists():                     # absent when cleanly checkpointed
            shutil.copyfile(side, Path(str(dest) + suffix))
    return dest


def collect_mac(since_unix: float, tz, debug: bool):
    """Return (records, new_watermark_unix). records: list of payload dicts."""
    if not KNOWLEDGE_DB.exists():
        log(f"Mac: knowledgeC.db not found at {KNOWLEDGE_DB} — skipping Mac source")
        problem("mac", "source_missing", f"not found: {KNOWLEDGE_DB}")
        return [], since_unix

    since_cocoa = since_unix - COCOA_OFFSET
    tmpdir = tempfile.mkdtemp(prefix="wellness-kc-")
    try:
        try:
            snapshot = _snapshot_knowledge_db(tmpdir)
        except OSError as exc:
            # macOS TCC surfaces a missing Full Disk Access grant here now that
            # we copy before opening (it used to surface at connect time).
            log(f"Mac: cannot read knowledgeC.db ({exc}). Grant Full Disk Access to "
                f"the interpreter running this script (see the .plist header).")
            denied = getattr(exc, "errno", None) == 1 or "not permitted" in str(exc).lower()
            problem("mac", "no_full_disk_access" if denied else "read_error", str(exc))
            return [], since_unix

        try:
            conn = sqlite3.connect(snapshot)
        except sqlite3.Error as exc:
            log(f"Mac: cannot open knowledgeC.db snapshot ({exc}).")
            problem("mac", "read_error", str(exc))
            return [], since_unix

        return _read_mac_rows(conn, since_unix, since_cocoa, tz, debug)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _read_mac_rows(conn, since_unix: float, since_cocoa: float, tz, debug: bool):
    try:
        rows = conn.execute(
            """
            SELECT ZVALUESTRING, ZSTARTDATE, ZENDDATE
            FROM ZOBJECT
            WHERE ZSTREAMNAME = '/app/usage'
              AND ZENDDATE > ?
              AND ZVALUESTRING IS NOT NULL
            ORDER BY ZSTARTDATE
            """,
            (since_cocoa,),
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        # Reading a private copy now, so this is a corrupt/torn snapshot rather
        # than a permissions problem — the next run re-copies and recovers.
        log(f"Mac: query failed on knowledgeC snapshot ({exc}).")
        conn.close()
        return [], since_unix
    conn.close()

    records = []
    watermark = since_unix
    for bundle, zstart, zend in rows:
        if zstart is None or zend is None:
            continue
        start_unix = zstart + COCOA_OFFSET
        end_unix = zend + COCOA_OFFSET
        duration = int(round(end_unix - start_unix))
        if duration <= 0:
            continue
        records.append({
            "app": bundle,
            "duration_sec": duration,
            "start_time": unix_to_local_iso(start_unix, tz),
            "end_time": unix_to_local_iso(end_unix, tz),
            "device": "Mac",
        })
        watermark = max(watermark, end_unix)
        dbg(debug, f"  Mac  {bundle:32s} {duration:6d}s  {unix_to_local_iso(start_unix, tz)}")

    log(f"Mac: {len(records)} new segment(s) from knowledgeC")
    return records, watermark


# ── iPhone source: Biome app.InFocus (SEGB) ─────────────────────────────────
def _read_varint(data: bytes, i: int):
    shift = 0
    result = 0
    while i < len(data):
        b = data[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, i
        shift += 7
        if shift > 63:
            break
    raise ValueError("varint overrun")


def _iter_pb_fields(data: bytes):
    """Yield (field_number, wire_type, raw_value) for a protobuf message.
    raw_value is an int (varint), or bytes (len-delimited / 64-bit / 32-bit)."""
    i = 0
    n = len(data)
    while i < n:
        try:
            key, i = _read_varint(data, i)
        except ValueError:
            return
        field = key >> 3
        wtype = key & 7
        try:
            if wtype == 0:
                val, i = _read_varint(data, i)
            elif wtype == 1:
                val = data[i:i + 8]; i += 8
            elif wtype == 2:
                ln, i = _read_varint(data, i)
                val = data[i:i + ln]; i += ln
            elif wtype == 5:
                val = data[i:i + 4]; i += 4
            else:
                return  # groups / unknown wire type: stop scanning this message
        except ValueError:
            return
        if wtype in (1, 5, 2) and len(val) == 0 and wtype != 2:
            return
        yield field, wtype, val


# app.InFocus record layout (reverse-engineered from real data, 2026-07):
#   f4 (64-bit double) = event time (Cocoa/Mac-absolute seconds)
#   f6 (string)        = the app bundle id in focus
#   f3 (varint)        = focus flag: 1 = came to foreground, 0 = left foreground
#   f1 (string)        = transition reason (SBSourceApplication:…, backlight, …)
# Each app produces paired f3=1 (start) → f3=0 (end) events; duration is the
# interval between them, and screen-off/idle time emits no events, so it's
# naturally excluded (no cap needed). There is NO device-id field — instead each
# SEGB FILE is a single device's stream, attributed by its bundle namespace.
_COCOA_EPOCH = datetime(2001, 1, 1)

# Bundle-namespace fingerprints used to attribute a whole SEGB file to a device.
_WATCH_MARKERS = ("carousel", "watchkitapp", "watchapp", "nanocalendar", "nanomaps",
                  "nanomail", "activitymonitorapp", "sessiontrackerapp", ".nano")
_MAC_MARKERS = ("loginwindow", "claudefordesktop", "iterm2", "systempreferences",
                ".desktop", "macsys", "com.apple.finder", "followupui", "com.apple.preview")
_IPHONE_MARKERS = ("com.apple.springboard", "mobilesafari", "sleeplockscreen",
                   "com.apple.camera", "burbn.instagram", "com.apple.webapp",
                   "atebits.tweetie2", "com.apple.mobile")


def _pb_fieldmap(data: bytes):
    """Top-level protobuf field map {field_number: (wire_type, raw_value)} (first wins)."""
    fm = {}
    for f, w, v in _iter_pb_fields(data):
        if f not in fm:
            fm[f] = (w, v)
    return fm


def _decode_focus_record(data: bytes):
    """Return (event_datetime_naive_utc, bundle, focus_flag) or None.
    time from f4 (Cocoa double), bundle from f6, focus_flag from f3 (1=start/0=end)."""
    fm = _pb_fieldmap(data)
    if 6 not in fm or fm[6][0] != 2 or 4 not in fm or fm[4][0] != 1:
        return None
    try:
        bundle = fm[6][1].decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not bundle or "." not in bundle:
        return None
    (secs,) = struct.unpack("<d", fm[4][1])
    t = _COCOA_EPOCH + timedelta(seconds=secs)
    if not (datetime(2020, 1, 1) < t < datetime(2028, 1, 1)):
        return None
    flag = fm[3][1] if (3 in fm and fm[3][0] == 0) else None
    return t, bundle, flag


def _classify_device(bundle_counts) -> str:
    """Attribute a SEGB file to 'iphone' | 'mac' | 'watch' | 'unknown' by dominant
    bundle namespace (files are single-device)."""
    score = {"watch": 0, "mac": 0, "iphone": 0}
    for b, c in bundle_counts.items():
        bl = b.lower()
        if any(k in bl for k in _WATCH_MARKERS):
            score["watch"] += c
        elif any(k in bl for k in _MAC_MARKERS):
            score["mac"] += c
        elif any(k in bl for k in _IPHONE_MARKERS):
            score["iphone"] += c
    return max(score, key=score.get) if any(score.values()) else "unknown"


def _is_excluded_bundle(bundle: str) -> bool:
    """Screen-transition pseudo-bundles that aren't real app usage."""
    return "backlight" in bundle.lower()


def _iter_segb_files():
    """Yield every SEGB file under the app.InFocus stream dirs.

    We read them ALL each run (only a few dozen small files) rather than
    filtering by mtime: Biome can append newly-synced data to a file without
    advancing its mtime (observed: a file with events through 7/14 whose mtime
    read 7/12), so an mtime filter could silently miss freshly-synced iPhone
    data — turning normal sync lag into a permanent gap. The watermark + the
    server's UNIQUE upsert keep this cheap regardless of how many files we read."""
    for base in BIOME_DIRS:
        if base.exists():
            for p in sorted(base.rglob("*")):
                if p.is_file():
                    yield p


def collect_iphone(since_unix: float, tz, debug: bool):
    """Return (records, new_watermark_unix). iPhone app-focus sessions from Biome
    app.InFocus, using f3=1→f3=0 pairing per bundle for accurate durations."""
    if not _SEGB_AVAILABLE:
        log(f"iPhone: SEGB parser unavailable ({_SEGB_IMPORT_ERROR}) — skipping iPhone source")
        problem("iphone", "parser_missing", str(_SEGB_IMPORT_ERROR))
        return [], since_unix

    files = list(_iter_segb_files())
    if not files:
        log("iPhone: no recent app.InFocus Biome files (is Screen Time 'Share Across "
            "Devices' on, and does this process have Full Disk Access?) — skipping")
        # Without Full Disk Access the Biome directory LOOKS empty, so when the Mac source
        # was just denied, that is the likelier cause here too.
        code = ("no_full_disk_access" if PROBLEMS.get("mac", {}).get("code") == "no_full_disk_access"
                else "no_biome_files")
        problem("iphone", code, "no recent app.InFocus Biome files")
        return [], since_unix

    # 1) Read every file, classify by device, keep events only from iPhone files.
    events = []            # (event_dt_naive_utc, bundle, focus_flag)
    iphone_files = mac_files = watch_files = unreadable = 0
    for path in files:
        # The Biome stream directory also holds non-SEGB housekeeping files (e.g.
        # a sync-daemon "lock" file) — skip those silently rather than counting
        # them as "unreadable", which is reserved for files that DO look like
        # SEGB but fail to parse (a real problem worth surfacing).
        try:
            is_segb = file_matches_segbv1_signature(path) or file_matches_segbv2_signature(path)
        except OSError:
            continue
        if not is_segb:
            dbg(debug, f"  iPhone: skipping non-SEGB file {path.name}")
            continue
        try:
            entries = list(read_segb_file(path))
        except Exception as exc:
            unreadable += 1
            dbg(debug, f"  iPhone: unreadable {path.name}: {exc}")
            continue
        decoded = []
        counts = {}
        for entry in entries:
            state = getattr(entry, "state", None)
            if state is not None and int(state) != int(EntryState.Written):
                continue
            rec = _decode_focus_record(getattr(entry, "data", b""))
            if rec is None:
                continue
            decoded.append(rec)
            counts[rec[1]] = counts.get(rec[1], 0) + 1
        if not decoded:
            continue
        device = _classify_device(counts)
        if device == "iphone":
            iphone_files += 1
            events.extend(decoded)
        elif device == "mac":
            mac_files += 1
        elif device == "watch":
            watch_files += 1
        dbg(debug, f"  iPhone: {path.name} -> {device} ({len(decoded)} focus events)")

    if not events:
        log(f"iPhone: SEGB files present but no iPhone focus events "
            f"(iphone_files={iphone_files} mac={mac_files} watch={watch_files} "
            f"unreadable={unreadable}). Re-run with --debug to inspect.")
        return [], since_unix

    # 2) Merge + sort all iPhone events, pair f3=1 (start) -> f3=0 (end) per
    #    bundle into foreground sessions.
    events.sort(key=lambda e: e[0])
    MAX_SESSION = timedelta(hours=2)     # guard against an unclosed pair
    open_start = {}                      # bundle -> start datetime (naive UTC)
    records = []
    watermark = since_unix
    sessions = 0

    def _finish(bundle, start_dt, end_dt):
        """Emit one foreground session if valid; advance the watermark."""
        nonlocal watermark, sessions
        dur = end_dt - start_dt
        if not (timedelta(0) < dur <= MAX_SESSION):
            return
        if _is_excluded_bundle(bundle):
            return
        start_unix = start_dt.replace(tzinfo=timezone.utc).timestamp()
        if start_unix <= since_unix:
            return  # already sent on a prior run
        end_unix = end_dt.replace(tzinfo=timezone.utc).timestamp()
        records.append({
            "app": bundle,
            "duration_sec": int(round(dur.total_seconds())),
            "start_time": unix_to_local_iso(start_unix, tz),
            "end_time": unix_to_local_iso(end_unix, tz),
            "device": "iPhone",
        })
        watermark = max(watermark, start_unix)
        sessions += 1
        dbg(debug, f"    session {bundle:30s} {int(dur.total_seconds()):5d}s  {start_dt}")

    for ev_dt, bundle, flag in events:
        if flag == 1:
            # close-on-reopen guard: if a prior session for this bundle was never
            # closed (a dropped f3=0), end it at this new start rather than
            # silently overwriting it (which would lose that foreground time).
            if bundle in open_start:
                _finish(bundle, open_start[bundle], ev_dt)
            open_start[bundle] = ev_dt
        elif flag == 0 and bundle in open_start:
            _finish(bundle, open_start.pop(bundle), ev_dt)

    log(f"iPhone: {len(records)} new session(s) from {iphone_files} iPhone file(s) "
        f"(mac_files={mac_files} watch_files={watch_files} unreadable={unreadable})")
    return records, watermark


# ── server POST ─────────────────────────────────────────────────────────────
def build_ssl_context():
    if os.environ.get("WELLNESS_VERIFY_TLS", "1") == "0":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        log("TLS: verification DISABLED (WELLNESS_VERIFY_TLS=0)")
        return ctx
    ca = os.environ.get("WELLNESS_CA_BUNDLE")
    if not ca:
        bundled = _HERE / "ca.crt"
        if bundled.exists():
            ca = str(bundled)
    if ca and os.path.exists(ca):
        return ssl.create_default_context(cafile=ca)
    return ssl.create_default_context()  # system trust (works with Apple's system python)


def post_records(records, base_url: str, token: str, ctx) -> dict:
    total = {"inserted": 0, "skipped": 0}
    url = base_url.rstrip("/") + "/api/screentime/ingest"
    for start in range(0, len(records), POST_CHUNK):
        chunk = records[start:start + POST_CHUNK]
        body = json.dumps(chunk).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        total["inserted"] += payload.get("inserted", 0)
        total["skipped"] += payload.get("skipped", 0)
    return total


def post_status(base_url: str, token: str, ctx, collected: int) -> None:
    """Report this run's per-source problems (empty = healthy). Best-effort: a failure
    here must never stop collection, and it is retried by the next run anyway."""
    body = json.dumps({"problems": PROBLEMS, "collected": collected,
                       "interpreter": os.path.realpath(sys.executable)}).encode("utf-8")
    req = urllib.request.Request(base_url.rstrip("/") + "/api/screentime/status",
                                 data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15):
            pass
    except Exception as exc:   # noqa: BLE001 — status is advisory
        log(f"WARN: could not report status to the server ({exc})")


# ── main ────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="Collect Screen Time and push to wellness-app")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--mac-only", action="store_true")
    ap.add_argument("--iphone-only", action="store_true")
    ap.add_argument("--since", type=float, default=None,
                    help="Look back this many DAYS instead of using the watermark")
    ap.add_argument("--first-days", type=float, default=7.0,
                    help="Look-back window (days) used only when no watermark exists")
    args = ap.parse_args()

    tz = local_tz()
    state = load_state()
    now_unix = datetime.now(timezone.utc).timestamp()

    def source_since(key: str) -> float:
        if args.since is not None:
            return now_unix - args.since * 86400
        if key in state:
            return float(state[key])
        return now_unix - args.first_days * 86400

    do_mac = not args.iphone_only
    do_iphone = not args.mac_only

    all_records = []
    new_state = dict(state)

    if do_mac:
        mac_since = source_since("mac_last_end_unix")
        mac_records, mac_wm = collect_mac(mac_since, tz, args.debug)
        all_records += mac_records
        new_state["mac_last_end_unix"] = mac_wm

    if do_iphone:
        iph_since = source_since("iphone_last_start_unix")
        iph_records, iph_wm = collect_iphone(iph_since, tz, args.debug)
        all_records += iph_records
        new_state["iphone_last_start_unix"] = iph_wm

    # Freshness per source. The two sources lag for completely different reasons:
    # knowledgeC is local (should be near-live), while the iPhone's Biome stream
    # only arrives when iOS decides to sync it to this Mac — a cadence Apple does
    # not document. Logging the lag each run turns that from guesswork into a
    # measurable distribution.
    for label, key in (("Mac", "mac_last_end_unix"), ("iPhone", "iphone_last_start_unix")):
        wm = new_state.get(key)
        if wm:
            log(f"{label} freshness: newest event {unix_to_local_iso(float(wm), tz)} "
                f"(lag {_fmt_lag(now_unix - float(wm))})")

    log(f"Collected {len(all_records)} new segment(s) total.")

    # Report status on EVERY real run — including the empty ones, which is exactly when
    # something is wrong (note 79).
    token = os.environ.get("WELLNESS_WIDGET_TOKEN", "").strip()
    base_url = os.environ.get("WELLNESS_URL", DEFAULT_URL)
    ctx = build_ssl_context()
    if token and not args.dry_run:
        post_status(base_url, token, ctx, len(all_records))

    if not all_records:
        log("Nothing to send.")
        if not args.dry_run:
            save_state(new_state)   # advance watermarks even when empty
        return 0

    if args.dry_run:
        log("DRY RUN — not posting. Sample of up to 10 records:")
        for r in all_records[:10]:
            log(f"  {r['device']:6s} {r['app']:32s} {r['duration_sec']:6d}s  {r['start_time']}")
        return 0

    if not token:
        log("ERROR: WELLNESS_WIDGET_TOKEN is not set. Aborting without advancing state.")
        return 2

    try:
        result = post_records(all_records, base_url, token, ctx)
    except urllib.error.HTTPError as exc:
        log(f"ERROR: server returned HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}")
        return 3
    except (urllib.error.URLError, ssl.SSLError) as exc:
        log(f"ERROR: could not reach {base_url} ({exc}). State NOT advanced; will retry next run.")
        return 3

    log(f"Posted to {base_url}: inserted={result['inserted']} skipped={result['skipped']}")
    save_state(new_state)   # only advance watermarks after a successful POST
    return 0


if __name__ == "__main__":
    sys.exit(main())
