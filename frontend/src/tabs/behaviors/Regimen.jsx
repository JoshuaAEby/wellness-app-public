import { useState } from "react";
import { Card, Eyebrow, Foot } from "../../components/ui.jsx";
import SupplementStack from "../../components/SupplementStack.jsx";

/**
 * The supplement regimen — WHAT is being taken, and since when.
 *
 * Reference material rather than a behaviour, so it is collapsed by default: the daily
 * "supplements" checkbox already answers "did I take the stack today", and it sits at
 * ~99% adherence anyway. What this answers instead is what the stack CONTAINS, which no
 * boolean can say — `habit_supplements` lumps every compound together and can never
 * attribute an effect to one of them.
 *
 * Starting or stopping writes a dated Intervention, which is most of the point: without
 * a date, something begun six months ago is invisible to every chart and the
 * before/after question cannot even be asked.
 *
 * Restored 2026-08-17 — the Behaviors rebuild dropped it, and it survived only because
 * the orphaned Habits.jsx was still on disk.
 */
export default function Regimen() {
  const [open, setOpen] = useState(false);
  return (
    <Card>
      <button onClick={() => setOpen((v) => !v)} className="w-full text-left">
        <Eyebrow left="SUPPLEMENT REGIMEN" right={open ? "HIDE" : "SHOW"} />
      </button>
      {open ? (
        <div className="mt-2"><SupplementStack /></div>
      ) : (
        <p className="mt-2 text-[10.5px] leading-snug text-ink-3">
          What you take and since when. Starting or stopping records a dated
          intervention, so the before/after is answerable later.
        </p>
      )}
      {open && (
        <Foot>
          Distinct from the daily supplements checkbox: that says the stack was taken,
          this says what the stack is.
        </Foot>
      )}
    </Card>
  );
}
