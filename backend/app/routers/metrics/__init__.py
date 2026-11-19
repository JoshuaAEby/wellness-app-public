"""/api/metrics — split by area on 2026-10-21 (was one 2,711-line module).

  today      GET /today (the dashboard payload), /activity-for-date
  series     chart time series: daily, raw-trend, sleep, events, screentime, weather, history…
  training   workouts, RPE, zones, HR recovery, anchors, ACWR forecast
  energy     energy balance / TDEE, predicted-vs-actual weight calibration
  nutrition  nutrition consistency + scorecard
  summaries  AI summary refreshes, manual push triggers, weekly digest, usage, progress

The training recommendation lives in app/services/training_rec.py.
"""
from fastapi import APIRouter

from app.routers.metrics import energy, nutrition, series, summaries, today, training

router = APIRouter(prefix="/api/metrics", tags=["metrics"])
for _m in (today, series, training, summaries, nutrition, energy):
    router.include_router(_m.router)
