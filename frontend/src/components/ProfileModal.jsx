import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api.js";

// ── Unit helpers ──────────────────────────────────────────────────────────────

function cmToFeetInches(cm) {
  const totalInches = cm / 2.54;
  return { feet: Math.floor(totalInches / 12), inches: Math.round(totalInches % 12) };
}

function feetInchesToCm(feet, inches) {
  return Math.round((feet * 12 + inches) * 2.54 * 10) / 10;
}

// ── Component ─────────────────────────────────────────────────────────────────

export default function ProfileModal({ onClose }) {
  const qc = useQueryClient();
  const { data: settings } = useQuery({ queryKey: ["settings"], queryFn: api.getSettings });

  const [feet, setFeet]       = useState("");
  const [inches, setInches]   = useState("");
  const [birthYear, setBirthYear] = useState("");
  const [sex, setSex]         = useState("");

  const initialized = feet !== "";
  if (settings && !initialized) {
    if (settings.height_cm) {
      const { feet: f, inches: i } = cmToFeetInches(settings.height_cm);
      setFeet(String(f));
      setInches(String(i));
    } else {
      setFeet("5");
      setInches("10");
    }
    setBirthYear(settings.birth_year ? String(settings.birth_year) : "");
    setSex(settings.biological_sex ?? "");
  }

  const saveMutation = useMutation({
    mutationFn: () => {
      const f = parseInt(feet) || 0;
      const i = parseInt(inches) || 0;
      const height_cm = feetInchesToCm(f, i);
      return api.updateSettings({
        // Must send these to avoid clearing them (unconditional write)
        protein_goal_g: settings?.protein_goal_g ?? null,
        calorie_goal: settings?.calorie_goal ?? null,
        // Profile fields
        height_cm,
        birth_year: birthYear !== "" ? parseInt(birthYear) : undefined,
        biological_sex: sex || undefined,
      });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["settings"] });
      onClose();
    },
  });

  const currentYear = new Date().getFullYear();
  const age = birthYear ? currentYear - parseInt(birthYear) : null;

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/60 pb-16" onClick={onClose}>
      <div
        className="w-full max-w-md rounded-t-2xl bg-slate-800 p-6 pb-10"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-5">
          <h2 className="text-lg font-semibold text-white">Profile</h2>
          <p className="text-xs text-slate-500">Used for energy balance estimates</p>
        </div>

        <div className="space-y-5">

          {/* Height */}
          <div>
            <label className="mb-2 block text-xs font-medium text-slate-400">Height</label>
            <div className="flex items-center gap-3">
              <div className="flex items-center gap-2 flex-1">
                <input
                  type="number"
                  value={feet}
                  onChange={(e) => setFeet(e.target.value)}
                  min="3" max="8" step="1"
                  className="w-full rounded-lg bg-slate-700 px-4 py-2 text-white outline-none focus:ring-2 focus:ring-indigo-500 text-center"
                />
                <span className="text-slate-400 text-sm flex-shrink-0">ft</span>
              </div>
              <div className="flex items-center gap-2 flex-1">
                <input
                  type="number"
                  value={inches}
                  onChange={(e) => setInches(e.target.value)}
                  min="0" max="11" step="1"
                  className="w-full rounded-lg bg-slate-700 px-4 py-2 text-white outline-none focus:ring-2 focus:ring-indigo-500 text-center"
                />
                <span className="text-slate-400 text-sm flex-shrink-0">in</span>
              </div>
            </div>
            {feet && inches !== "" && (
              <p className="mt-1 text-xs text-slate-500">
                {feetInchesToCm(parseInt(feet) || 0, parseInt(inches) || 0)} cm
              </p>
            )}
          </div>

          {/* Birth year */}
          <div>
            <label className="mb-2 block text-xs font-medium text-slate-400">Birth year</label>
            <input
              type="number"
              value={birthYear}
              onChange={(e) => setBirthYear(e.target.value)}
              min="1940" max={currentYear - 10} step="1"
              placeholder={String(currentYear - 30)}
              className="w-full rounded-lg bg-slate-700 px-4 py-2 text-white placeholder-slate-500 outline-none focus:ring-2 focus:ring-indigo-500"
            />
            {age != null && age > 0 && (
              <p className="mt-1 text-xs text-slate-500">Age: {age}</p>
            )}
          </div>

          {/* Biological sex */}
          <div>
            <label className="mb-2 block text-xs font-medium text-slate-400">
              Biological sex
              <span className="ml-1 text-slate-600 font-normal normal-case">(for metabolic rate formula)</span>
            </label>
            <div className="flex gap-3">
              {["male", "female"].map((opt) => (
                <button
                  key={opt}
                  type="button"
                  onClick={() => setSex(opt)}
                  className={`flex-1 rounded-lg py-2 text-sm font-medium transition-colors ${
                    sex === opt
                      ? "bg-indigo-600 text-white"
                      : "bg-slate-700 text-slate-400 hover:text-white"
                  }`}
                >
                  {opt.charAt(0).toUpperCase() + opt.slice(1)}
                </button>
              ))}
            </div>
          </div>

          {saveMutation.isError && (
            <p className="text-sm text-rose-400">Failed to save — try again</p>
          )}

          <div className="flex gap-3">
            <button onClick={onClose} className="flex-1 rounded-lg bg-slate-700 py-2 text-slate-300">
              Cancel
            </button>
            <button
              onClick={() => saveMutation.mutate()}
              disabled={saveMutation.isPending}
              className="flex-1 rounded-lg bg-indigo-600 py-2 font-semibold text-white disabled:opacity-50"
            >
              {saveMutation.isPending ? "Saving…" : "Save"}
            </button>
          </div>

        </div>
      </div>
    </div>
  );
}
