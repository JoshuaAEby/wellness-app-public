import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../api.js";
import { shrinkImage } from "../imageUtil.js";
import { RhoMark } from "./ui.jsx";

// Class board → weight targets (note 91, 2026-10-29). The user sees the board ~5 minutes
// before class: one photo, ~10 s, then one line per exercise with the weight to pick.
// The model only READS the board and maps lines to his Hevy names; every number here
// comes from his own sets (services/class_board). Presented as Rho, the app's coach
// (2026-10-29: female-coded, avatar/personality later). Readiness is shown, not
// applied — no morning signal predicted his lifting (54 sessions). Easing off is his
// call: the Easy day switch shows each target WITHOUT this session's step up (easy_lb).

// The API downsizes past 1568 px anyway; a whiteboard stays legible well below that.
const BOARD_EDGE = 1568;

function Target({ rec, easy }) {
  if (!rec || rec.target_lb == null) return null;
  const assisted = rec.unit === "lb assist";
  const lb = easy && rec.easy_lb != null ? rec.easy_lb : rec.target_lb;
  const perHand = easy && rec.easy_lb != null ? rec.easy_per_hand : rec.per_hand;
  const eased = easy && rec.easy_lb != null && rec.easy_lb !== rec.target_lb;
  const main = assisted
    ? (lb === 0 ? "no band" : `${lb} lb band`)
    : `${lb} lb`;
  return (
    <span className="flex-none text-right">
      <span className="block text-[22px] font-semibold leading-none tabular-nums tracking-[-.02em] text-ink">
        {main}
      </span>
      {perHand != null && (
        <span className="mt-0.5 block text-[10px] tabular-nums text-ink-3">2 × {perHand}</span>
      )}
      {eased && (
        <span className="mt-0.5 block text-[10px] tabular-nums text-ink-3">easy · target {rec.target_lb}</span>
      )}
    </span>
  );
}

function Row({ item, idx, boardId, catalog, onChange, easy }) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const pick = useMutation({
    mutationFn: (exercise) => api.classBoardTarget({
      exercise, reps_target: item.reps_target, board_id: boardId, n: item.n, idx,
      implement: item.implement,
    }),
    onSuccess: (r) => {
      // His own pick: no longer "unsure".
      onChange({ ...item, match: r.exercise, type: r.type, rec: r.rec, match_confidence: "picked" });
      setOpen(false); setQ("");
    },
  });
  const hits = useMemo(() => {
    const s = q.trim().toLowerCase();
    if (!s) return [];
    return (catalog || []).filter((c) => c.title.toLowerCase().includes(s)).slice(0, 8);
  }, [q, catalog]);
  const rec = item.rec || {};
  const dim = !item.loadable && rec.target_lb == null;
  const choices = [...new Set([...(item.alternatives || [])].filter((a) => a !== item.match))];

  return (
    <div className={`border-t border-rule py-2.5 ${dim ? "opacity-60" : ""}`}>
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <p className="text-[13px] font-medium leading-snug text-ink">
            {item.name || item.board_text}
            {item.reps_target != null && (
              <span className="font-normal text-ink-3"> × {item.reps_target}</span>
            )}
            {/* The board's own notation when it isn't just "x15" — e.g. 5-5-5 L/R/both is
                10 per arm, MAX, laps — so the number above can be checked against it. */}
            {item.rep_scheme && !/^x?\s*\d+$/i.test(item.rep_scheme.trim()) && (
              <span className="font-normal text-ink-3"> ({item.rep_scheme})</span>
            )}
          </p>
          <button onClick={() => setOpen((o) => !o)}
            className="mt-0.5 text-left text-[10.5px] leading-snug text-ink-3 hover:text-ink-2">
            {item.match ? item.match : "not matched"}
            {item.match && item.match_confidence === "low" && " (unsure)"}
            {item.match_confidence === "learned" && " (learned)"} · change
          </button>
        </div>
        <Target rec={rec} easy={easy} />
      </div>
      <p className="prose-ui mt-1 text-[11px] leading-snug text-ink-2">{rec.text}</p>
      {open && (
        <div className="mt-2 rounded-lg bg-surface p-2">
          {choices.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {choices.map((a) => (
                <button key={a} onClick={() => pick.mutate(a)} disabled={pick.isPending}
                  className="rounded-full border border-rule px-2 py-1 text-[10.5px] text-ink-2 hover:text-ink">
                  {a}
                </button>
              ))}
            </div>
          )}
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search your exercises"
            className="mt-2 w-full rounded-md border border-rule bg-card px-2 py-1.5 text-[12px] text-ink placeholder:text-ink-3" />
          {hits.map((c) => (
            <button key={c.title} onClick={() => pick.mutate(c.title)} disabled={pick.isPending}
              className="block w-full py-1 text-left text-[11.5px] text-ink-2 hover:text-ink">
              {c.title} <span className="text-ink-3">· {c.sessions}×</span>
            </button>
          ))}
          {pick.isError && <p className="mt-1 text-[11px] text-over">{String(pick.error?.message || pick.error)}</p>}
        </div>
      )}
    </div>
  );
}

// Last checked class: Rho's targets vs what Hevy says he lifted (class_board.check_boards,
// run after each Hevy sync). Beats first — credit for real things, not a report card.
function LastClass({ last }) {
  const o = (last.outcome || []).filter((x) => ["beat", "hit", "under"].includes(x.verdict));
  const order = { beat: 0, hit: 1, under: 2 };
  o.sort((a, b) => order[a.verdict] - order[b.verdict]);
  const word = { beat: "beat", hit: "hit", under: "under" };
  return (
    <div className="mt-2 rounded-xl border border-rule bg-card p-3">
      <p className="text-[9.5px] font-semibold tracking-micro text-ink-3">
        LAST CLASS · {last.date.slice(5).replace("-", "/")} · {last.at_or_above} OF {last.judged} AT OR ABOVE TARGET
        {last.easy ? " · EASY DAY" : ""}
      </p>
      <div className="mt-1.5 space-y-0.5">
        {o.map((x) => (
          <p key={x.idx} className="text-[11px] tabular-nums text-ink-2">
            <span className="text-ink">{(x.title || x.board_text || "").replace(/ \((Dumbbell|Barbell|Kettlebell)\)$/, "")}</span>{" "}
            {x.actual_lb} vs {x.target_lb}{x.unit === "lb assist" ? " lb band" : " lb"}
            <span className="text-ink-3"> · {word[x.verdict]}</span>
          </p>
        ))}
      </div>
    </div>
  );
}

export default function ClassBoardSheet({ onClose }) {
  const [result, setResult] = useState(null);
  const [typing, setTyping] = useState(false);
  const [text, setText] = useState("");
  const [elapsed, setElapsed] = useState(0);
  const [easy, setEasy] = useState(false);
  const camRef = useRef(null);
  const libRef = useRef(null);
  const { data: last } = useQuery({
    queryKey: ["class-board-last"], queryFn: api.getClassBoardLast, staleTime: 10 * 60 * 1000,
  });
  const { data: catalog } = useQuery({
    queryKey: ["class-board-exercises"], queryFn: api.getClassBoardExercises,
    staleTime: 30 * 60 * 1000,
  });
  // Reopening the sheet shows today's board again (note 96) instead of asking for a new photo.
  const todayQ = useQuery({ queryKey: ["class-board-today"], queryFn: api.getClassBoardToday, staleTime: 0 });
  const restored = useRef(false);
  useEffect(() => {
    if (restored.current || result || !todayQ.data?.items) return;
    restored.current = true;
    setResult(todayQ.data);
    setEasy(!!todayQ.data.easy);
  }, [todayQ.data, result]);

  const parse = useMutation({
    mutationFn: async ({ file, text }) => {
      if (file) {
        let blob;
        try { blob = await shrinkImage(file, BOARD_EDGE, 0.88); }
        catch { throw new Error("Couldn't open that photo — retake it or type the board"); }
        return api.parseClassBoard({ blob });
      }
      return api.parseClassBoard({ text });
    },
    onSuccess: (r) => { restored.current = true; setResult(r); setTyping(false); setEasy(false); },
  });

  useEffect(() => {
    if (!parse.isPending) return undefined;
    setElapsed(0);
    const t0 = Date.now();
    const id = setInterval(() => setElapsed(Math.round((Date.now() - t0) / 1000)), 500);
    return () => clearInterval(id);
  }, [parse.isPending]);

  const onFile = (e) => {
    const f = e.target.files?.[0];
    e.target.value = "";
    if (f) parse.mutate({ file: f });
  };

  const setItem = (i, next) => setResult((r) => ({ ...r, items: r.items.map((x, k) => (k === i ? next : x)) }));

  // Circuits ("Q1", "Q2") keep their own heading; boards without them are one list.
  const groups = useMemo(() => {
    const out = [];
    (result?.items || []).forEach((it, i) => {
      const g = it.group || "";
      if (!out.length || out[out.length - 1].g !== g) out.push({ g, rows: [] });
      out[out.length - 1].rows.push({ it, i });
    });
    return out;
  }, [result]);

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-canvas">
      <div className="w-full max-w-md px-4 pb-12 pt-6">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="flex items-center gap-2 text-[17px] font-semibold text-ink"><RhoMark size={20} />Rho · class board</h2>
          <button onClick={onClose} className="text-[13px] text-ink-3 hover:text-ink">Done</button>
        </div>

        <input ref={camRef} type="file" accept="image/*" capture="environment" className="hidden" onChange={onFile} />
        <input ref={libRef} type="file" accept="image/*" className="hidden" onChange={onFile} />

        {!result && !parse.isPending && todayQ.isLoading && (
          <p className="text-[12px] text-ink-3">Loading…</p>
        )}

        {!result && !parse.isPending && !todayQ.isLoading && (
          <div className="space-y-2">
            {/* Two equal choices (note 96): "From photos" was a small grey link and went unseen. */}
            <div className="grid grid-cols-2 gap-2">
              <button onClick={() => camRef.current?.click()}
                className="rounded-xl border border-rule bg-card py-4 text-[14px] font-semibold text-ink">
                Take a photo
              </button>
              <button onClick={() => libRef.current?.click()}
                className="rounded-xl border border-rule bg-card py-4 text-[14px] font-semibold text-ink">
                Choose a photo
              </button>
            </div>
            <div className="flex justify-end text-[12px]">
              <button onClick={() => setTyping((t) => !t)} className="text-ink-3 hover:text-ink-2">Type it instead</button>
            </div>
            {typing && (
              <div>
                <textarea value={text} onChange={(e) => setText(e.target.value)} rows={6}
                  placeholder={"1. DB floor press x15\n2. KB sumo rows x15\n3. Chin ups x MAX"}
                  className="w-full rounded-lg border border-rule bg-card p-2 text-[13px] text-ink placeholder:text-ink-3" />
                <button onClick={() => parse.mutate({ text })} disabled={!text.trim()}
                  className="mt-1 w-full rounded-lg border border-rule bg-surface py-2 text-[13px] font-medium text-ink disabled:opacity-40">
                  Get targets
                </button>
              </div>
            )}
            {last && last.judged > 0 && <LastClass last={last} />}
            <p className="prose-ui pt-2 text-[11px] leading-snug text-ink-3">
              Rho reads the board, matches each line to your Hevy exercises, and picks a weight
              from your own sets since April: one step up when every set reached the target
              last time, otherwise the same weight. She takes about 10 seconds.
            </p>
          </div>
        )}

        {parse.isPending && (
          <div className="rounded-xl border border-rule bg-card p-4 text-center">
            <p className="text-[13px] text-ink">Rho is reading the board… {elapsed}s</p>
            <div className="mt-3 h-1 overflow-hidden rounded-full bg-surface">
              <div className="h-full w-1/3 animate-pulse rounded-full bg-ink-3" />
            </div>
          </div>
        )}

        {parse.isError && (
          <p className="mt-3 text-[12px] text-over">{String(parse.error?.message || parse.error)}</p>
        )}

        {result && (
          <div>
            <div className="flex items-baseline justify-between gap-3 text-[9.5px] font-semibold tracking-micro text-ink-3">
              <span className="whitespace-nowrap">{result.items.length} EXERCISES</span>
              {result.readiness && (
                <span className="whitespace-nowrap">READINESS {result.readiness.score} · NOT APPLIED</span>
              )}
            </div>
            <div className="mt-2 flex items-center justify-between gap-3">
              <p className="prose-ui text-[11px] leading-snug text-ink-2">
                {easy ? "Easy day: last session's weights, no step up."
                      : "First set misses the reps by 2+? Drop one step for the rest."}
              </p>
              <button
                onClick={() => { const v = !easy; setEasy(v); api.setClassBoardEasy(result.id, v).catch(() => {}); }}
                className={`flex-none rounded-full border px-2.5 py-1 text-[11px] ${easy ? "border-ink-3 text-ink" : "border-rule text-ink-3"}`}>
                {easy ? "✓ Easy day" : "Easy day"}
              </button>
            </div>
            {groups.map(({ g, rows }) => (
              <div key={g || "all"} className="mt-2">
                {g && <p className="text-[10px] font-semibold tracking-micro text-ink-3">{g}</p>}
                {rows.map(({ it, i }) => (
                  <Row key={i} item={it} idx={i} boardId={result.id} catalog={catalog} easy={easy}
                    onChange={(next) => setItem(i, next)} />
                ))}
              </div>
            ))}
            {result.unreadable?.length > 0 && (
              <p className="mt-2 text-[11px] text-ink-3">Couldn't read: {result.unreadable.join("; ")}</p>
            )}
            <div className="mt-4 flex justify-between border-t border-rule pt-3 text-[12px]">
              <button onClick={() => setResult(null)}
                className="text-ink-3 hover:text-ink-2">New board</button>
              {result.ms != null && <span className="text-ink-3">{(result.ms / 1000).toFixed(1)} s</span>}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
