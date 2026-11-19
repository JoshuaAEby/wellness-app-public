import { useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api.js";
import { prepareScreenshot } from "../imageUtil.js";

function localToday() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

const emptyRow = () => ({ marker: "", label: "", value: "", unit: "", ref_low: "", ref_high: "" });

// A row needs the user's eyes when the two parse arms disagreed on the measurement
// itself (value/unit) or one arm missed the marker entirely, and they haven't
// resolved it yet. Reference-range disagreements are `low` and don't gate saving —
// they mislabel a marker as in/out of range but don't corrupt the trend.
const needsReview = (r) => r.severity === "high" && !r.reviewed;

const FIELD_LABEL = { value: "Value", unit: "Unit", ref_low: "Ref low", ref_high: "Ref high" };

// Screenshot limits, checked BEFORE upload so the message names the fix. Counted after
// tall captures are split into tiles (imageUtil.prepareScreenshot). The server enforces
// its own, looser, caps (routers/blood MAX_BLOOD_IMAGES*).
const MAX_SHOTS = 20;
const MAX_SHOTS_BYTES = 15 * 1024 * 1024;
const fmtBytes = (b) => (b >= 1024 * 1024 ? `${(b / 1024 / 1024).toFixed(1)} MB` : `${Math.round(b / 1024)} KB`);

// Add a blood panel: upload the lab PDF (parsed twice — vision + text — and any
// disagreement surfaced for review), upload screenshots of it (several images → ONE
// panel, read by two different models; 2026-10-28), paste the report text, or enter
// rows manually.
// Rows are editable before saving; saves upsert on (date, marker) so re-uploading
// the same report is idempotent.
export default function BloodPanelModal({ onClose }) {
  const [mode, setMode] = useState("pdf");   // pdf | images | paste | manual
  const [text, setText] = useState("");
  const [date, setDate] = useState(localToday());
  const [rows, setRows] = useState([]);
  const [parsed, setParsed] = useState(false);
  const [srcText, setSrcText] = useState(null);   // extracted PDF text, for verification
  const [showSrc, setShowSrc] = useState(false);
  const [review, setReview] = useState(null);     // {high, low, total, cross_checked}
  const [sources, setSources] = useState(null);   // per-arm status, for the banner
  const [dateDiff, setDateDiff] = useState(null);
  const [onlyIssues, setOnlyIssues] = useState(false);
  const [confirmUnreviewed, setConfirmUnreviewed] = useState(false);
  const [elapsed, setElapsed] = useState(0);      // seconds spent on the current parse
  // Screenshots, in panel order: {id, name, blob, url, width, height}. The blob is the
  // downscaled JPEG actually sent, so the review shows exactly what the models read.
  const [shots, setShots] = useState([]);
  const [shotErrs, setShotErrs] = useState([]);
  const [preparing, setPreparing] = useState(false);
  const [viewImg, setViewImg] = useState(null);   // 0-based index shown in the check panel
  const shotsRef = useRef(shots);
  const checkRef = useRef(null);
  const qc = useQueryClient();
  shotsRef.current = shots;
  useEffect(() => () => shotsRef.current.forEach(s => URL.revokeObjectURL(s.url)), []);

  const parse = useMutation({
    mutationFn: () => api.parseBloodReport(text),
    onSuccess: (data) => {
      if (data.date) setDate(data.date);
      setRows((data.markers ?? []).map(m => ({
        marker: m.marker ?? "", label: m.label ?? m.marker ?? "",
        value: m.value ?? "", unit: m.unit ?? "",
        ref_low: m.ref_low ?? "", ref_high: m.ref_high ?? "",
      })));
      setParsed(true);
    },
  });

  // PDF upload feeds the SAME confirmation step as paste — one review path. Paste
  // returns bare markers (single parse, no cross-check); PDF adds per-row status /
  // severity / alt, which are simply absent on the paste path.
  const applyParsed = (data) => {
    if (data.date) setDate(data.date);
    setRows((data.markers ?? []).map(m => ({
      marker: m.marker ?? "", label: m.label ?? m.marker ?? "",
      value: m.value ?? "", unit: m.unit ?? "",
      ref_low: m.ref_low ?? "", ref_high: m.ref_high ?? "",
      status: m.status, severity: m.severity, diffs: m.diffs ?? [], alt: m.alt ?? null,
      image: m.image ?? null, dups: m.dups ?? null,
      reviewed: false,
    })));
    setParsed(true);
  };
  const parsePdf = useMutation({
    mutationFn: (file) => api.parseBloodPdf(file),
    onMutate: () => setElapsed(0),
    onSuccess: (data) => {
      setSrcText(data.text_preview ?? null);
      setReview(data.review ?? null);
      setSources(data.sources ?? null);
      setDateDiff(data.date_diff ?? null);
      setOnlyIssues((data.review?.high ?? 0) > 0);   // land straight on the problems
      applyParsed(data);
    },
  });
  const parseImages = useMutation({
    mutationFn: () => api.parseBloodImages(
      shots.map(s => ({ blob: s.blob, name: s.part ? `${s.name} (${s.part})` : s.name }))),
    onMutate: () => setElapsed(0),
    onSuccess: (data) => {
      setSrcText(null);
      setReview(data.review ?? null);
      setSources(data.sources ?? null);
      setDateDiff(data.date_diff ?? null);
      setOnlyIssues((data.review?.high ?? 0) > 0);
      setViewImg(null);
      applyParsed(data);
    },
  });

  // Decode → downscale (and tile tall captures) → append, in the order picked. A file
  // the browser can't decode is listed by name rather than failing the whole pick.
  const addShots = async (files) => {
    setPreparing(true);
    const added = [], errs = [];
    for (const f of files) {
      try {
        const tiles = await prepareScreenshot(f);
        tiles.forEach((t, k) => added.push({
          id: `${Date.now()}-${Math.random()}`,
          name: f.name, part: tiles.length > 1 ? `part ${k + 1}/${tiles.length}` : null,
          blob: t.blob, url: URL.createObjectURL(t.blob), width: t.width, height: t.height,
        }));
      } catch (e) {
        errs.push(e.message);
      }
    }
    setShots(ss => [...ss, ...added]);
    setShotErrs(errs);
    setPreparing(false);
  };
  const moveShot = (i, d) => setShots(ss => {
    const j = i + d;
    if (j < 0 || j >= ss.length) return ss;
    const next = [...ss];
    [next[i], next[j]] = [next[j], next[i]];
    return next;
  });
  const removeShot = (i) => setShots(ss => {
    URL.revokeObjectURL(ss[i].url);
    return ss.filter((_, j) => j !== i);
  });
  const shotBytes = shots.reduce((a, s) => a + s.blob.size, 0);
  const shotLimit = shots.length > MAX_SHOTS
    ? `${shots.length} images — max ${MAX_SHOTS} per panel (tall scrolling captures count once per tile). Remove some.`
    : shotBytes > MAX_SHOTS_BYTES
      ? `${fmtBytes(shotBytes)} after compression — max ${fmtBytes(MAX_SHOTS_BYTES)}. Remove some.`
      : null;

  const isImages = sources?.kind === "images";
  // Which reading each diff slot holds: vision/text for a PDF, the two models for images.
  const arms = sources?.arms ?? { pdf: "vision", text: "text" };
  const openImage = (k) => {
    setViewImg(k);
    requestAnimationFrame(() => checkRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" }));
  };

  const save = useMutation({
    mutationFn: () => api.saveBloodPanel(
      date,
      rows
        .filter(r => (r.marker || r.label) && r.value !== "" && !isNaN(parseFloat(r.value)))
        .map(r => ({
          marker: r.marker || r.label.toLowerCase().replace(/[^a-z0-9]+/g, "_"),
          label: r.label || r.marker,
          value: parseFloat(r.value),
          unit: r.unit || null,
          ref_low: r.ref_low === "" ? null : parseFloat(r.ref_low),
          ref_high: r.ref_high === "" ? null : parseFloat(r.ref_high),
        })),
    ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["blood-markers"] });
      onClose();
    },
  });

  // The upload takes ~20s (two model calls, the Opus vision arm dominating) with no
  // server-side progress to report. A live second count is the honest signal that it
  // is working rather than wedged — anything resembling a determinate progress bar
  // would be inventing information we don't have.
  const parsing = parsePdf.isPending || parseImages.isPending;
  useEffect(() => {
    if (!parsing) return;
    const id = setInterval(() => setElapsed(s => s + 1), 1000);
    return () => clearInterval(id);
  }, [parsing]);

  // Typing over a flagged field IS the review — the user has looked at it and made
  // a call, so stop nagging about that row.
  // `[field]: v` must come LAST — with it first, an explicit setRow(i,"reviewed",true)
  // would be clobbered by the computed `reviewed` that follows.
  const setRow = (i, field, v) =>
    setRows(rs => rs.map((r, j) => (j === i
      ? { ...r, reviewed: r.reviewed || field === "value" || field === "unit", [field]: v }
      : r)));

  // Resolve one disagreeing field by taking a specific arm's reading.
  const pick = (i, field, which) =>
    setRows(rs => rs.map((r, j) => {
      if (j !== i) return r;
      const d = (r.diffs ?? []).find(x => x.field === field);
      if (!d) return r;
      const v = d[which];
      const next = { ...r, [field]: v ?? "", picked: { ...(r.picked ?? {}), [field]: which } };
      // Reviewed once every hard field has an explicit pick.
      const hard = (r.diffs ?? []).filter(x => x.field === "value" || x.field === "unit");
      next.reviewed = hard.every(x => (next.picked ?? {})[x.field]);
      return next;
    }));

  const validRows = rows.filter(r => (r.marker || r.label) && r.value !== "").length;
  const unreviewed = rows.filter(needsReview).length;
  const flagged = rows.filter(r => r.severity === "high" || r.severity === "low").length;
  // Carry the ORIGINAL index through the filter — setRow/pick address rows by
  // position in `rows`, so filtering a plain array would misdirect every edit.
  const shownRows = rows
    .map((r, i) => [r, i])
    .filter(([r]) => !onlyIssues || r.severity === "high" || r.severity === "low");

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/60 overflow-y-auto p-4"
      onClick={onClose}>
      <div className="w-full max-w-md rounded-2xl bg-slate-800 p-5 my-8" onClick={e => e.stopPropagation()}>
        <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
          <h2 className="whitespace-nowrap text-base font-semibold text-white">Add blood panel</h2>
          <div className="flex gap-1">
            {["pdf", "images", "paste", "manual"].map(m => (
              <button key={m}
                onClick={() => { setMode(m); if (m === "manual" && rows.length === 0) setRows([emptyRow()]); }}
                className={`px-2.5 py-1 rounded text-xs font-medium capitalize ${
                  mode === m ? "bg-indigo-600 text-white" : "bg-slate-700 text-slate-400"
                }`}
              >{m}</button>
            ))}
          </div>
        </div>

        <label className="block text-xs text-slate-400 mb-1">Draw date</label>
        <input type="date" value={date} onChange={e => setDate(e.target.value)}
          className="mb-3 rounded-lg bg-slate-700 px-3 py-2 text-sm text-white outline-none" />

        {mode === "pdf" && !parsed && (
          <>
            <label className={`flex flex-col items-center justify-center rounded-xl border-2 border-dashed bg-slate-900/40 px-4 py-8 text-center ${
              parsePdf.isPending ? "cursor-wait border-indigo-600/60"
                                 : "cursor-pointer border-slate-600 hover:border-indigo-500"}`}>
              {parsePdf.isPending ? (
                <>
                  <span className="text-2xl animate-breathe">🧪</span>
                  <span className="mt-2 text-sm font-medium text-slate-200">
                    Reading it twice… {elapsed}s
                  </span>
                  {/* Indeterminate by design — there is no progress signal to report. */}
                  <span className="mt-3 block h-1 w-40 overflow-hidden rounded-full bg-slate-700">
                    <span className="block h-full w-1/4 rounded-full bg-indigo-500 animate-indeterminate" />
                  </span>
                  <span className="mt-2.5 text-[11px] leading-relaxed text-slate-500">
                    {elapsed < 35
                      ? "Vision and text parses are running side by side — usually about 20 seconds."
                      : "Still going — long or dense reports take a while. It hasn't stalled."}
                  </span>
                </>
              ) : (
                <>
                  <span className="text-2xl">📄</span>
                  <span className="mt-2 text-sm font-medium text-slate-200">
                    Choose a lab PDF
                  </span>
                  <span className="mt-1 text-[11px] leading-relaxed text-slate-500">
                    Read twice — once from the page images, once from the text layer — and
                    any disagreement is flagged for you before anything is saved.
                    Takes about 20 seconds.
                  </span>
                </>
              )}
              <input
                type="file"
                accept="application/pdf,.pdf"
                className="hidden"
                disabled={parsePdf.isPending}
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (f) parsePdf.mutate(f);
                  e.target.value = "";          // allow re-picking the same file
                }}
              />
            </label>
            {parsePdf.isError && (
              <p className="mt-2 text-xs leading-relaxed text-rose-400">
                {parsePdf.error?.message || "Upload failed"}
              </p>
            )}
            <p className="mt-2 text-[10px] leading-relaxed text-slate-600">
              Scans and photos work too, but they have no text layer — so only one
              parse runs and there's no cross-check to catch a misread.
            </p>
          </>
        )}

        {mode === "images" && !parsed && (
          <>
            {parseImages.isPending ? (
              <div className="flex flex-col items-center justify-center rounded-xl border-2 border-dashed border-indigo-600/60 bg-slate-900/40 px-4 py-8 text-center">
                <span className="text-2xl animate-breathe">🧪</span>
                <span className="mt-2 text-sm font-medium text-slate-200">
                  Reading {shots.length} image{shots.length === 1 ? "" : "s"} twice… {elapsed}s
                </span>
                {/* Indeterminate by design — there is no progress signal to report. */}
                <span className="mt-3 block h-1 w-40 overflow-hidden rounded-full bg-slate-700">
                  <span className="block h-full w-1/4 rounded-full bg-indigo-500 animate-indeterminate" />
                </span>
                <span className="mt-2.5 text-[11px] leading-relaxed text-slate-500">
                  {elapsed < 45
                    ? "Two models read all the screenshots side by side — usually 20–40 seconds."
                    : "Still going — many screenshots take a while. It hasn't stalled."}
                </span>
              </div>
            ) : (
              <label className={`flex flex-col items-center justify-center rounded-xl border-2 border-dashed bg-slate-900/40 px-4 text-center ${
                shots.length ? "py-4" : "py-8"} ${
                preparing ? "cursor-wait border-indigo-600/60" : "cursor-pointer border-slate-600 hover:border-indigo-500"}`}>
                <span className="text-2xl">🖼️</span>
                <span className="mt-2 text-sm font-medium text-slate-200">
                  {preparing ? "Preparing…" : shots.length ? "Add more screenshots" : "Choose screenshots"}
                </span>
                {!shots.length && (
                  <span className="mt-1 text-[11px] leading-relaxed text-slate-500">
                    Pick every screenshot of ONE report — PNG, JPEG or HEIC, several at once.
                    They're read together as one panel; overlapping rows are counted once.
                  </span>
                )}
                <input
                  type="file"
                  multiple
                  accept="image/png,image/jpeg,image/webp,image/heic,image/heif,.heic,.heif"
                  className="hidden"
                  disabled={preparing}
                  onChange={(e) => {
                    const fs = Array.from(e.target.files ?? []);
                    e.target.value = "";          // allow re-picking the same files
                    if (fs.length) addShots(fs);
                  }}
                />
              </label>
            )}

            {shots.length > 0 && (
              <ol className="mt-2 space-y-1">
                {shots.map((sh, i) => (
                  <li key={sh.id} className="flex items-center gap-2 rounded-lg bg-slate-900/50 px-2 py-1.5">
                    <span className="w-4 shrink-0 text-right text-[11px] tabular-nums text-slate-500">{i + 1}</span>
                    <img src={sh.url} alt="" className="h-10 w-7 shrink-0 rounded object-cover object-top" />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-xs text-slate-200">{sh.name}</span>
                      <span className="block text-[10px] text-slate-500">
                        {sh.part && <span className="text-slate-400">{sh.part} · </span>}
                        {sh.width}×{sh.height} · {fmtBytes(sh.blob.size)}
                      </span>
                    </span>
                    <button onClick={() => moveShot(i, -1)} disabled={i === 0 || parseImages.isPending}
                      aria-label="Move up"
                      className="h-7 w-7 shrink-0 rounded text-slate-400 hover:bg-slate-700 disabled:opacity-20">↑</button>
                    <button onClick={() => moveShot(i, 1)} disabled={i === shots.length - 1 || parseImages.isPending}
                      aria-label="Move down"
                      className="h-7 w-7 shrink-0 rounded text-slate-400 hover:bg-slate-700 disabled:opacity-20">↓</button>
                    <button onClick={() => removeShot(i)} disabled={parseImages.isPending}
                      aria-label="Remove"
                      className="h-7 w-7 shrink-0 rounded text-slate-600 hover:text-rose-400 disabled:opacity-20">✕</button>
                  </li>
                ))}
              </ol>
            )}
            {shotErrs.map(m => (
              <p key={m} className="mt-1.5 text-xs leading-relaxed text-rose-400">{m}</p>
            ))}
            {shots.length > 0 && (
              <>
                <p className={`mt-2 text-[11px] leading-relaxed ${shotLimit ? "text-rose-400" : "text-slate-500"}`}>
                  {shotLimit ?? `${shots.length} image${shots.length === 1 ? "" : "s"} · ${fmtBytes(shotBytes)} — in report order (top of the report first).`}
                </p>
                <button
                  onClick={() => parseImages.mutate()}
                  disabled={!!shotLimit || preparing || parseImages.isPending}
                  className="mt-2 w-full rounded-xl bg-indigo-600 py-2.5 text-sm font-semibold text-white disabled:opacity-40"
                >
                  Read as one panel
                </button>
              </>
            )}
            {parseImages.isError && (
              <p className="mt-2 text-xs leading-relaxed text-rose-400">
                {parseImages.error?.message || "Upload failed"}
              </p>
            )}
            <p className="mt-2 text-[10px] leading-relaxed text-slate-600">
              Screenshots have no text layer, so the cross-check is a second model reading
              the same images: it catches one model's slip, not a digit both misread.
            </p>
          </>
        )}

        {mode === "paste" && !parsed && (
          <>
            <textarea
              value={text}
              onChange={e => setText(e.target.value)}
              rows={9}
              placeholder="Paste the whole results table from your lab portal / PDF — any format. Every quantitative marker will be extracted for review before saving."
              className="w-full rounded-xl bg-slate-900 border border-slate-700 px-3 py-2.5 text-xs text-white placeholder-slate-600 outline-none focus:border-indigo-500 font-mono"
            />
            {parse.isError && <p className="mt-1 text-xs text-rose-400">Parse failed — try again.</p>}
            <button
              onClick={() => parse.mutate()}
              disabled={!text.trim() || parse.isPending}
              className="mt-3 w-full rounded-xl bg-indigo-600 py-2.5 text-sm font-semibold text-white disabled:opacity-40"
            >
              {parse.isPending ? "Parsing…" : "Parse report"}
            </button>
          </>
        )}

        {/* Reconciliation banner — says plainly whether a cross-check happened, and
            never lets "one arm read it" look like "two arms agreed". */}
        {parsed && review && (
          <div className={`mb-3 rounded-lg border px-3 py-2.5 ${
            review.high > 0 ? "border-amber-600/50 bg-amber-950/30"
            : !review.cross_checked ? "border-slate-600 bg-slate-900/50"
            : "border-emerald-700/40 bg-emerald-950/20"
          }`}>
            {!review.cross_checked ? (
              <>
                <p className="text-xs font-medium text-slate-300">
                  Read once — not cross-checked
                </p>
                <p className="mt-1 text-[11px] leading-relaxed text-slate-500">
                  {sources?.text?.reason ?? "only one parse ran"}, so there was no second
                  reading to compare against. Check these values against the {isImages ? "screenshots" : "PDF"} yourself.
                </p>
              </>
            ) : review.high > 0 ? (
              <>
                <p className="text-xs font-medium text-amber-300">
                  {review.high} {review.high === 1 ? "marker needs" : "markers need"} your check
                </p>
                <p className="mt-1 text-[11px] leading-relaxed text-amber-200/60">
                  {isImages
                    ? "Two models read the same screenshots and disagreed here, or a marker appeared twice with different values. Pick the correct reading, or type your own."
                    : "Two independent parses (vision + text) disagreed here. Pick the correct reading, or type your own."}
                  {review.low > 0 && ` ${review.low} minor reference-range difference${review.low === 1 ? "" : "s"} also flagged.`}
                </p>
              </>
            ) : (
              <p className="text-xs text-emerald-300/90">
                {isImages ? `${arms.pdf} and ${arms.text} agreed` : "Two independent parses agreed"} on all {review.total} markers
                {review.low > 0 && ` (${review.low} reference-range difference${review.low === 1 ? "" : "s"} flagged)`}.
                {/* Same images in both arms — agreement can't rule out a digit both misread. */}
                {isImages && <span className="text-emerald-300/60"> Both read the same images, so glance at anything blurry.</span>}
              </p>
            )}
            {dateDiff && (
              <p className="mt-1.5 text-[11px] text-amber-300">
                Draw date disagreed: {dateDiff.pdf ?? "—"} ({arms.pdf}) vs {dateDiff.text ?? "—"} ({arms.text}) — confirm above.
              </p>
            )}
            {flagged > 0 && (
              <button
                onClick={() => setOnlyIssues(v => !v)}
                className="mt-2 text-[11px] font-medium text-indigo-300 hover:text-indigo-200"
              >
                {onlyIssues ? `Show all ${rows.length} markers` : `Show only the ${flagged} flagged`}
              </button>
            )}
          </div>
        )}

        {parsed && srcText && (
          <div className="mb-3 rounded-lg border border-slate-700 bg-slate-900/50">
            <button
              onClick={() => setShowSrc(v => !v)}
              className="flex w-full items-center justify-between px-3 py-2 text-left"
            >
              <span className="text-[11px] text-slate-400">
                Check against the PDF text {showSrc ? "▾" : "▸"}
              </span>
              <span className="text-[10px] text-slate-600">verify before saving</span>
            </button>
            {showSrc && (
              <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words px-3 pb-3 font-mono text-[10px] leading-relaxed text-slate-400">
                {srcText}
              </pre>
            )}
          </div>
        )}

        {/* The screenshot equivalent of "Check against the PDF text": the images the
            models actually read (downscaled), and which extracted rows each one
            produced — so a value can be checked against its source, not from memory. */}
        {parsed && isImages && shots.length > 0 && (
          <div ref={checkRef} className="mb-3 rounded-lg border border-slate-700 bg-slate-900/50">
            <button
              onClick={() => setViewImg(v => (v === null ? 0 : null))}
              className="flex w-full items-center justify-between px-3 py-2 text-left"
            >
              <span className="text-[11px] text-slate-400">
                Check against the screenshots {viewImg !== null ? "▾" : "▸"}
              </span>
              <span className="text-[10px] text-slate-600">verify before saving</span>
            </button>
            {viewImg !== null && shots[viewImg] && (
              <div className="px-3 pb-3">
                <div className="mb-2 flex gap-1.5 overflow-x-auto pb-1">
                  {shots.map((sh, k) => (
                    <button key={sh.id} onClick={() => setViewImg(k)}
                      className={`relative shrink-0 overflow-hidden rounded border-2 ${
                        k === viewImg ? "border-indigo-500" : "border-transparent opacity-60"}`}>
                      <img src={sh.url} alt={`Image ${k + 1}`} className="h-12 w-9 object-cover object-top" />
                      <span className="absolute bottom-0 right-0 rounded-tl bg-black/70 px-1 text-[9px] text-white">{k + 1}</span>
                    </button>
                  ))}
                </div>
                {(() => {
                  const here = rows.filter(r => r.image === viewImg + 1);
                  return (
                    <p className="mb-2 text-[10px] leading-relaxed text-slate-400">
                      <span className="text-slate-500">Read from image {viewImg + 1}: </span>
                      {here.length
                        ? here.map(r => `${r.label || r.marker} ${r.value}${r.unit ? ` ${r.unit}` : ""}`).join(" · ")
                        : "nothing"}
                    </p>
                  );
                })()}
                <div className="max-h-[60vh] overflow-y-auto rounded bg-white">
                  <img src={shots[viewImg].url} alt={`Image ${viewImg + 1}`} className="block w-full" />
                </div>
              </div>
            )}
          </div>
        )}

        {(mode === "manual" || parsed) && (
          <>
            <div className="max-h-72 overflow-y-auto space-y-1.5 pr-1">
              <div className="grid gap-1.5 text-[10px] text-slate-500 uppercase tracking-wide"
                style={{ gridTemplateColumns: isImages ? "1.25rem 1fr 4.5rem 3.5rem 1.5rem" : "1fr 4.5rem 3.5rem 1.5rem" }}>
                {isImages && <span>#</span>}
                <span>Marker</span><span>Value</span><span>Unit</span><span />
              </div>
              {shownRows.map(([r, i]) => {
                const open = needsReview(r);
                const hasDiffs = (r.diffs?.length ?? 0) > 0;
                return (
                  <div key={i}
                    className={open ? "rounded-lg border-l-2 border-amber-500 bg-amber-950/20 py-1 pl-1.5"
                      : hasDiffs ? "rounded-lg border-l-2 border-slate-600 py-1 pl-1.5"
                      : ""}>
                    {/* min-w-0 on every grid child: grid items default to
                        min-width:auto, so without it a long value or chip row pushes
                        the whole grid wider than the modal and clips the ✕ column. */}
                    <div className="grid gap-1.5" style={{ gridTemplateColumns: isImages ? "1.25rem 1fr 4.5rem 3.5rem 1.25rem" : "1fr 4.5rem 3.5rem 1.25rem" }}>
                      {/* Source image: tap to see the screenshot this value was read from. */}
                      {isImages && (r.image
                        ? <button onClick={() => openImage(r.image - 1)} title={`Read from image ${r.image}`}
                            className="min-w-0 rounded bg-slate-700/70 text-[10px] tabular-nums text-slate-300 hover:bg-slate-600">{r.image}</button>
                        : <span className="min-w-0 self-center text-center text-[10px] text-slate-600">—</span>)}
                      <input value={r.label} onChange={e => setRow(i, "label", e.target.value)}
                        placeholder="e.g. ApoB"
                        className="min-w-0 rounded-lg bg-slate-900 border border-slate-700 px-2 py-1.5 text-xs text-white outline-none" />
                      <input value={r.value} onChange={e => setRow(i, "value", e.target.value)}
                        inputMode="decimal"
                        className={`min-w-0 rounded-lg bg-slate-900 border px-2 py-1.5 text-xs text-white outline-none text-right ${
                          open ? "border-amber-600" : "border-slate-700"}`} />
                      <input value={r.unit} onChange={e => setRow(i, "unit", e.target.value)}
                        className="min-w-0 rounded-lg bg-slate-900 border border-slate-700 px-2 py-1.5 text-xs text-slate-300 outline-none" />
                      <button onClick={() => setRows(rs => rs.filter((_, j) => j !== i))}
                        className="min-w-0 text-slate-600 hover:text-rose-400 text-sm">✕</button>
                    </div>

                    {/* One arm found this marker and the other didn't — nothing to pick
                        between, so just say which, and let the ✕ drop it if it's junk. */}
                    {(r.status === "missing_in_text" || r.status === "missing_in_pdf") && (
                      <p className="mt-1 text-[10px] leading-relaxed text-amber-300/80">
                        Only the {r.status === "missing_in_text" ? arms.pdf : arms.text} parse found this
                        marker — confirm it's real, or remove the row.
                        {!r.reviewed && (
                          <button onClick={() => setRow(i, "reviewed", true)}
                            className="ml-1.5 rounded bg-slate-700 px-1.5 py-0.5 font-medium text-slate-200 hover:bg-slate-600">
                            keep
                          </button>
                        )}
                      </p>
                    )}

                    {/* The same marker read twice from overlapping screenshots, with different
                        values — not an overlap, a misread or two analytes under one name.
                        Show every reading with its image; the user types or keeps. */}
                    {r.status === "duplicate_in_images" && (
                      <p className="mt-1 text-[10px] leading-relaxed text-amber-300/80">
                        Read more than once with different values:{" "}
                        {(r.dups ?? []).map((d, k) => (
                          <span key={k}>
                            {k > 0 && ", "}
                            <span className="font-mono">{d.value ?? "—"}{d.unit ? ` ${d.unit}` : ""}</span>
                            {d.image && (
                              <button onClick={() => openImage(d.image - 1)}
                                className="ml-1 rounded bg-slate-700/70 px-1 text-slate-300 hover:bg-slate-600">img {d.image}</button>
                            )}
                          </span>
                        ))}
                        . Check the screenshots and fix the value.
                        {!r.reviewed && (
                          <button onClick={() => setRow(i, "reviewed", true)}
                            className="ml-1.5 rounded bg-slate-700 px-1.5 py-0.5 font-medium text-slate-200 hover:bg-slate-600">
                            keep
                          </button>
                        )}
                      </p>
                    )}

                    {/* Field-level disagreements: show both readings, tap to take one. */}
                    {hasDiffs && r.diffs.map(d => {
                      const picked = r.picked?.[d.field];
                      const chip = (which) => (
                        <button key={which} onClick={() => pick(i, d.field, which)}
                          className={`rounded px-1.5 py-0.5 font-mono ${
                            picked === which ? "bg-indigo-600 text-white"
                              : "bg-slate-700/70 text-slate-300 hover:bg-slate-600"}`}>
                          {d[which] ?? "—"}
                          <span className="ml-1 font-sans text-[9px] opacity-60">
                            {arms[which]}
                          </span>
                        </button>
                      );
                      return (
                        <div key={d.field} className="mt-1 flex flex-wrap items-center gap-1.5 text-[10px]">
                          <span className={d.field === "value" || d.field === "unit"
                            ? "text-amber-300/90" : "text-slate-500"}>
                            {FIELD_LABEL[d.field] ?? d.field}
                          </span>
                          {chip("pdf")}
                          {chip("text")}
                        </div>
                      );
                    })}
                  </div>
                );
              })}
              {shownRows.length === 0 && (
                <p className="py-3 text-center text-xs text-slate-500">
                  Nothing flagged.
                </p>
              )}
            </div>
            <button onClick={() => setRows(rs => [...rs, emptyRow()])}
              className="mt-2 text-xs text-indigo-400 hover:text-indigo-300">+ add marker</button>

            {save.isError && <p className="mt-1 text-xs text-rose-400">Save failed — try again.</p>}
            {/* Unresolved hard conflicts take one extra tap. These rows go into a
                series trended for years and nothing downstream can catch a wrong
                value later, so saving past a known disagreement should be a decision,
                not a reflex. */}
            <button
              onClick={() => {
                if (unreviewed > 0 && !confirmUnreviewed) { setConfirmUnreviewed(true); return; }
                save.mutate();
              }}
              disabled={validRows === 0 || save.isPending}
              className={`mt-3 w-full rounded-xl py-2.5 text-sm font-semibold text-white disabled:opacity-40 ${
                unreviewed > 0 ? "bg-amber-600" : "bg-emerald-600"}`}
            >
              {save.isPending ? "Saving…"
                : unreviewed > 0
                  ? (confirmUnreviewed
                      ? `Save anyway — ${unreviewed} unchecked`
                      : `${unreviewed} still unchecked`)
                  : `Save ${validRows} marker${validRows === 1 ? "" : "s"}`}
            </button>
            {unreviewed > 0 && !confirmUnreviewed && (
              <p className="mt-1.5 text-center text-[11px] leading-relaxed text-amber-300/70">
                Pick a reading for each flagged marker, or tap again to save as-is.
              </p>
            )}
            {parsed && (
              <button onClick={() => {
                setParsed(false); setRows([]); setReview(null); setSources(null);
                setSrcText(null); setDateDiff(null); setOnlyIssues(false);
                setConfirmUnreviewed(false); setViewImg(null);   // screenshots stay listed
              }}
                className="mt-2 w-full text-xs text-slate-500 hover:text-slate-300">← start over</button>
            )}
          </>
        )}

        <button onClick={onClose}
          className="mt-3 w-full rounded-lg bg-slate-700 py-2 text-sm text-slate-300 hover:bg-slate-600">
          Cancel
        </button>
      </div>
    </div>
  );
}
