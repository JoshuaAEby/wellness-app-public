from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    secret_key: str = "dev-secret-key-change-in-production"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24 * 30  # 30 days

    app_password: str = "changeme"
    webhook_secret: str = "changeme"

    database_url: str = "sqlite:////data/wellness.db"

    anthropic_api_key: str = ""

    hevy_api_key: str = ""      # Hevy Pro API key (hevy.com → Settings → Developer) — empty disables sync

    # Centralized usage logging — points to the household app's /api/usage/log endpoint
    household_usage_url: str = ""   # e.g. http://household.local:8092/api/usage/log
    household_usage_key: str = ""   # shared secret (USAGE_API_KEY on household side)

    # The household app's SQLite DB, mounted READ-ONLY (compose: household_data:/household:ro)
    # so recipes can be read into the food logger (note 37). Empty/missing file = the
    # My Recipes section simply reports itself unavailable; nothing else changes.
    household_db_path: str = "/household/household.db"

    tz: str = "America/New_York"

    ntfy_url: str = ""          # e.g. "https://ntfy.example.com" — leave empty to disable push
    ntfy_topic: str = "wellness-daily"

    # Static read-only token for the /api/widgets/summary endpoint (Widgy etc.)
    widget_token: str = "changeme"

    # Siri Shortcuts voice logging (docs/VOICE_LOGGING.md). Grants ONLY /api/voice/*;
    # empty = voice logging disabled (503), never open.
    voice_token: str = ""

    # Outdoor conditions (Open-Meteo — free, no API key). Both must be non-zero
    # or the weather service no-ops and the Today card simply doesn't render.
    weather_lat: float = 0.0
    weather_lon: float = 0.0

    # Home Assistant environment-sensor bridge (bedroom temp/humidity → nightly
    # health_metrics rows). All three must be set or the bridge no-ops.
    # ha_env_entities format: "sensor.entity_id:metric_key,sensor.other:other_key"
    ha_url: str = ""           # e.g. http://homeassistant.local:8123
    ha_token: str = ""         # HA long-lived access token (HA profile → Security)
    ha_env_entities: str = ""  # e.g. sensor.bedroom_temperature:bedroom_temp,sensor.bedroom_humidity:bedroom_humidity
    # Presence gate: bedroom aggregates are only written for nights this entity
    # was home ≥50% of the sleep window (away nights = not sleeping in that room
    # = contaminated data). Empty disables the gate. Also feeds slept_away and
    # wfh_day (weekday 9–17 home fraction).
    ha_presence_entity: str = ""  # e.g. person.me
    # Partner presence overnight (partner_present night-input, home nights only)
    ha_partner_entity: str = ""   # e.g. person.partner
    # Evening TV: minutes playing/on/paused in the 2h before bedtime (bedtime-
    # relative beats a fixed clock cutoff — bedtime varies). Empty disables.
    # A smart TV's media_player typically reports the streaming app in use; between
    # sessions it may flap off/unavailable. See services/tv.
    ha_tv_entity: str = ""        # e.g. media_player.living_room
    # Nintendo parental controls: minutes played today on the console, all players.
    ha_switch_entity: str = ""


settings = Settings()


# ── Refuse to run on shipped defaults (2026-10-21, backlog #19) ───────────────
# A missing or unreadable .env silently falls back to the defaults above, which would
# mean a public JWT signing key, password "changeme" and an open-ish webhook. main.py's
# lifespan refuses to start instead; a deploy that hits this fails its healthcheck and
# rolls back. ALLOW_INSECURE_SECRETS=1 is for unit tests and throwaway local containers.
_INSECURE = {"", "changeme", "dev-secret-key-change-in-production"}
_MIN_LEN = {"secret_key": 32, "app_password": 12, "webhook_secret": 16}


def insecure_secrets(s: "Settings") -> list[str]:
    """Names (never values) of required secrets that are defaults or too short."""
    return [k for k, n in _MIN_LEN.items()
            if (getattr(s, k) or "") in _INSECURE or len(getattr(s, k) or "") < n]
