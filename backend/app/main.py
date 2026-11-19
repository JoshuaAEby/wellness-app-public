import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.routers import auth_router, health_data, metrics, manual_entry, food_log, settings, streaks, widgets, insights
from app.services.scheduler import start_scheduler, stop_scheduler

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
# httpx logs the full request URL at INFO — which would write the home lat/lon of
# the hourly weather fetch into the container logs. Quiet it to WARNING.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)
# Every ERROR line is counted by kind into error_events (app/error_ledger.py → Data Health).
from app import error_ledger  # noqa: E402
error_ledger.install()

STATIC_DIR = Path(__file__).parent.parent / "static"

# Schema is owned entirely by Alembic (see alembic/versions/). The container CMD
# runs `alembic upgrade head` before starting uvicorn. There is intentionally no
# in-app migration step here — the previous hand-rolled `_MIGRATIONS` list was
# removed when the schema was consolidated into Alembic.


@asynccontextmanager
async def lifespan(app: FastAPI):
    import os
    from app.config import insecure_secrets, settings as app_config   # `settings` here is the router
    bad = insecure_secrets(app_config)
    if bad and os.environ.get("ALLOW_INSECURE_SECRETS") != "1":
        raise RuntimeError(
            f"Refusing to start: {', '.join(bad)} unset, default or too short — check backend/.env "
            "(ALLOW_INSECURE_SECRETS=1 overrides, for tests only)")
    if bad:
        logger.warning("Running with insecure secrets (%s) — ALLOW_INSECURE_SECRETS=1", ", ".join(bad))
    from app.timeutils import check_process_tz
    check_process_tz()
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="Wellness App", version="0.1.0", lifespan=lifespan)

# Offline-queue replays apply at most once (app/idempotency.py).
from app.idempotency import IdempotencyMiddleware  # noqa: E402
app.add_middleware(IdempotencyMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API routers
app.include_router(auth_router.router)
app.include_router(health_data.router)
from app.routers import admin as admin_router
app.include_router(admin_router.router)
from app.routers import habits as habits_router
app.include_router(habits_router.router)
app.include_router(metrics.router)
app.include_router(manual_entry.router)
app.include_router(food_log.router)
app.include_router(settings.router)
app.include_router(streaks.router)
app.include_router(widgets.router)
app.include_router(insights.router)
from app.routers import interventions as interventions_router
app.include_router(interventions_router.router)
from app.routers import hevy as hevy_router
app.include_router(hevy_router.router)
from app.routers import experiments as experiments_router
app.include_router(experiments_router.router)
from app.routers import blood as blood_router
app.include_router(blood_router.router)
from app.routers import screentime as screentime_router
app.include_router(screentime_router.router)
from app.routers import day_flags as day_flags_router
app.include_router(day_flags_router.router)
from app.routers import caffeine as caffeine_router
app.include_router(caffeine_router.router)

from app.routers import notes as notes_router
app.include_router(notes_router.router)
from app.routers import telemetry as telemetry_router
app.include_router(telemetry_router.router)
from app.routers import my_foods as my_foods_router
app.include_router(my_foods_router.router)

from app.routers import recipes as recipes_router   # My Recipes (note 37, Alembic 0068)
app.include_router(recipes_router.router)

from app.routers import intake as intake_router
app.include_router(intake_router.alcohol_router)
app.include_router(intake_router.water_router)
app.include_router(intake_router.nap_router)
app.include_router(intake_router.intake_router)
from app.routers import game as game_router
app.include_router(game_router.router)
from app.routers import pvt as pvt_router
app.include_router(pvt_router.router)
from app.routers import battery as battery_router
app.include_router(battery_router.router)
from app.routers import food_tags as food_tags_router
app.include_router(food_tags_router.router)
from app.routers import supplements as supplements_router
app.include_router(supplements_router.router)
from app.routers import freshness as freshness_router
app.include_router(freshness_router.router)
from app.routers import voice as voice_router  # noqa: E402
app.include_router(voice_router.router)   # Siri Shortcuts (docs/VOICE_LOGGING.md)
from app.routers import tz as tz_router  # noqa: E402  (tz awareness phase 1, 0073)
app.include_router(tz_router.router)
from app.routers import class_board as class_board_router  # noqa: E402  (note 91, 0083)
app.include_router(class_board_router.router)
from app.routers import health_events as health_events_router  # noqa: E402  (note 93, 0086)
app.include_router(health_events_router.router)
from app.routers import tv as tv_router  # noqa: E402  (note 85, 0088)
app.include_router(tv_router.router)


# Serve React SPA — must be registered after all API routes
if STATIC_DIR.exists():

    @app.middleware("http")
    async def _static_cache_headers(request, call_next):
        """Make index.html revalidate, and let hashed assets cache forever.

        Neither had ANY cache directive, so the browser fell back to heuristic
        caching and happily served a stale index.html — which still points at the
        PREVIOUS bundle hash. The new bundle was deployed and served correctly; the
        client just never asked for it. That is why a deploy could look like it had
        not happened, and it would have hit every deploy, not one.

        Vite fingerprints everything under /assets (index-CyrJp8Qy.js), so those are
        immutable by construction and safe to cache for a year — the filename changes
        when the content does. index.html is the one file whose URL is stable and
        whose content changes every build, so it is the one that must revalidate.
        `no-cache` means "revalidate before use", not "do not store": with an ETag
        the usual answer is a 304, so this costs one small conditional request per
        load rather than a re-download.
        """
        response = await call_next(request)
        path = request.url.path
        if path.startswith("/assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif path == "/" or not path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    # PWA icons and manifest — must be served as real files, not SPA fallback
    _icons_dir = STATIC_DIR / "icons"
    if _icons_dir.exists():
        app.mount("/icons", StaticFiles(directory=_icons_dir), name="icons")

    @app.get("/manifest.json", include_in_schema=False)
    async def serve_manifest():
        p = STATIC_DIR / "manifest.json"
        return FileResponse(str(p), media_type="application/manifest+json") if p.exists() else FileResponse(str(STATIC_DIR / "index.html"))

    # The offline service worker (frontend/public/sw.js). Must be a real file at the site
    # ROOT (its scope is its own path) and must revalidate on every load — the
    # middleware above gives it `no-cache` — or a fixed worker could never replace itself.
    @app.get("/sw.js", include_in_schema=False)
    async def serve_sw():
        return FileResponse(str(STATIC_DIR / "sw.js"), media_type="application/javascript")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str):
        index = STATIC_DIR / "index.html"
        return FileResponse(str(index))
else:
    logger.warning("Static directory %s not found — frontend not served", STATIC_DIR)
