// node --test frontend/tests — offline queue time-stamping (src/offline.js, 2026-10-21).
import test from "node:test";
import assert from "node:assert/strict";
import { stampTime, isQueueable } from "../src/offline.js";
import { foodToday } from "../src/utils.js";

// The FOOD day, as stampTime compares against — not the calendar date. They differ
// 00:00–03:59 local, and the calendar version failed CI whenever it ran then
// (2026-10-27 00:37 UTC on the UTC runner).
const today = foodToday();

test("snacks get the tap time, main meals keep their slot", () => {
  const s = stampTime("/api/nutrition/logs/batch", { meal_type: "snack", date: today, items: [] });
  assert.match(s.at_time, /^\d{2}:\d{2}$/);
  const b = stampTime("/api/nutrition/logs/batch", { meal_type: "breakfast", date: today, items: [] });
  assert.equal(b.at_time, undefined);
  const past = stampTime("/api/nutrition/logs/batch", { meal_type: "snack", date: "2020-01-01", items: [] });
  assert.equal(past.at_time, undefined);           // a backfilled day isn't "now"
});

test("caffeine gets a naive-UTC timestamp; an explicit one is kept", () => {
  assert.match(stampTime("/api/caffeine/logs", { kind: "cb" }).timestamp, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/);
  assert.equal(stampTime("/api/caffeine/logs", { timestamp: "2026-01-01T12:00:00" }).timestamp, "2026-01-01T12:00:00");
});

test("only creates are queueable", () => {
  assert.equal(isQueueable("/api/water/logs", "POST"), true);
  assert.equal(isQueueable("/api/water/logs/5", "PATCH"), false);
  assert.equal(isQueueable("/api/nutrition/logs/12", "DELETE"), false);
  assert.equal(isQueueable("/api/settings", "POST"), false);
});
