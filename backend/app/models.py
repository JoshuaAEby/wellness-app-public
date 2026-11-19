from datetime import datetime

from sqlalchemy import (
    Boolean,
    JSON,
    Column,
    DateTime,
    Date,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from app.database import Base


class HealthMetric(Base):
    __tablename__ = "health_metrics"

    id = Column(Integer, primary_key=True)
    metric_type = Column(String, nullable=False)
    timestamp = Column(DateTime, nullable=False)
    value = Column(Float, nullable=False)
    unit = Column(String)
    source = Column(String)
    # HOW it was entered: 'voice', or NULL = the app (0071). Never a new `source` value —
    # the HAE upsert protects rows by source == 'wellness_app'.
    entry_channel = Column(String)

    __table_args__ = (
        # The unique constraint's own index already covers (metric_type, timestamp);
        # the separate ix_health_metrics_type_ts duplicated it and was dropped in 0074.
        UniqueConstraint("metric_type", "timestamp", name="uq_health_metrics_type_ts"),
    )


class SleepSession(Base):
    __tablename__ = "sleep_sessions"

    id = Column(Integer, primary_key=True)
    date = Column(Date, unique=True, nullable=False)
    bedtime = Column(DateTime)
    wake_time = Column(DateTime)
    total_min = Column(Integer)
    deep_min = Column(Integer)
    core_min = Column(Integer)
    rem_min = Column(Integer)
    awake_min = Column(Integer)
    source = Column(String)


class Workout(Base):
    __tablename__ = "workouts"

    id = Column(Integer, primary_key=True)
    apple_uuid = Column(String, unique=True, nullable=False)
    activity_type = Column(String)
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime)
    duration_sec = Column(Integer)
    active_calories = Column(Float)
    avg_hr = Column(Float)
    max_hr = Column(Float)
    hr_samples = Column(JSON)
    source = Column(String)
    rpe = Column(Float)       # session RPE 1–10, manually entered
    intensity_met = Column(Float)  # metabolic intensity kcal/hr·kg (≈ MET), from Apple Health
    trimp = Column(Float)     # Banister TRIMP (HR-based internal load), computed in metrics_engine
    zone1_min = Column(Float) # Minutes below Zone 2 (<115 bpm). Added 2026-08-06 — without
                              # it ~35% of HR-tracked workout time belonged to no zone at
                              # all (walks were ~100% unaccounted), so the zone minutes
                              # never summed to the session duration.
    zone2_min = Column(Float) # Minutes in Zone 2 (115–135 bpm), computed from HR series in metrics_engine
    zone3_min = Column(Float) # Minutes in Zone 3 (135–155 bpm)
    zone4_min = Column(Float) # Minutes in Zone 4 (threshold). Split out 2026-08-08.
    zone5_min = Column(Float) # Minutes in Zone 5 (VO2max work)
    # RETAINED as zone4_min + zone5_min. Not dropped: it is a registered correlation
    # input (high_intensity_pct_7d) with cached pairs, and dropping it would discard
    # them. The split is purely additive.
    high_intensity_min = Column(Float) # Minutes in Zone 4+5 combined
    # A watch workout started and never stopped. Flagged rather than deleted or
    # truncated: the real session isn't recoverable, so any duration we invented
    # would be fabrication, while deleting would erase that anything happened.
    # Excluded from load and zone aggregates; see services/workout_qc.
    # HR recovery. `hr_peak` is the max in the final 2 minutes; `hr_recovery_60` is that
    # peak minus the lowest HR 50-70 s after the session ends. Store BOTH — HRR is not
    # comparable across sessions without its peak (see Alembic 0054).
    hr_peak = Column(Float)
    hr_recovery_60 = Column(Float)
    suspect = Column(Boolean, default=False)
    suspect_reason = Column(String)


class DailyMetric(Base):
    __tablename__ = "daily_metrics"

    id = Column(Integer, primary_key=True)
    date = Column(Date, unique=True, nullable=False)
    daily_load = Column(Float)
    atl = Column(Float)
    ctl = Column(Float)
    acwr = Column(Float)
    hrv_value = Column(Float)
    hrv_baseline = Column(Float)
    hrv_z_score = Column(Float)
    rhr_value = Column(Float)
    rhr_baseline = Column(Float)
    rhr_z_score = Column(Float)
    sleep_debt_hours = Column(Float)
    wrist_temp_deviation = Column(Float)
    zone1_pct_7d = Column(Float)  # Zone 1 % (<115 bpm) over rolling 7-day window
    # Zone percentages are shares of MEASURED HR time (they sum to 100), not of
    # duration_sec — see metrics_engine. This column reports what fraction of logged
    # training time actually produced HR samples, so a thin-evidence split is visible.
    hr_coverage_pct_7d = Column(Float)
    zone2_pct_7d = Column(Float)  # Zone 2 % of training time over rolling 7-day window
    zone3_pct_7d = Column(Float)  # Zone 3 % (135–155 bpm) over rolling 7-day window
    zone4_pct_7d = Column(Float)  # Zone 4 % over rolling 7-day window
    zone5_pct_7d = Column(Float)  # Zone 5 % over rolling 7-day window
    high_intensity_pct_7d = Column(Float)  # Zone 4+5 combined (kept — correlation input)
    readiness_score = Column(Float)
    readiness_label = Column(String)
    ai_summary = Column(Text)
    # JSON: date + value of each key input as the briefing read them (0072, note 57).
    # Internal provenance only — never rendered into ai_summary.
    ai_summary_inputs = Column(Text)
    computed_at = Column(DateTime, default=datetime.utcnow)


class BodyWeightLog(Base):
    __tablename__ = "body_weight_log"

    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, nullable=False, default=datetime.utcnow)
    weight_kg = Column(Float, nullable=False)
    notes = Column(String)


class BodyMeasurement(Base):
    """Tape measurements, one row per local date (cm). Added 2026-09-12.

    Exists because the scale's body-fat % is ~a function of weight (R^2 0.990 over 26
    weeks) and so cannot see recomposition. Waist down at flat weight = fat loss; arm
    or chest up with waist flat = muscle. See alembic 0061.
    """
    __tablename__ = "body_measurements"

    id = Column(Integer, primary_key=True)
    date = Column(Date, unique=True, nullable=False)
    waist_cm = Column(Float)
    arm_cm = Column(Float)
    chest_cm = Column(Float)
    notes = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


class BodyScan(Base):
    """Segmental (hand + foot) bioimpedance scans, e.g. an InBody 270 at the gym. Added
    2026-10-29, alembic 0082. One row per local date; values in LB as printed.

    Kept apart from health_metrics on purpose: the home scale's body-fat % is ~a function
    of weight, and its lean mass ran a steady ~14 lb above the InBody at two scans
    six months apart. A scan is a check on the cheap instrument, not another sample of
    it — mixing them would make the daily series jump by 14 lb on scan days.
    `segments`: {"left_arm", "right_arm", "trunk", "left_leg", "right_leg"} lean lb.
    """
    __tablename__ = "body_scans"

    id = Column(Integer, primary_key=True)
    date = Column(Date, unique=True, nullable=False)
    time = Column(String)              # "HH:MM" local, as printed (hydration varies by hour)
    device = Column(String)
    weight_lb = Column(Float, nullable=False)
    body_fat_pct = Column(Float, nullable=False)
    fat_mass_lb = Column(Float)        # derived weight × PBF when not entered
    lean_mass_lb = Column(Float)       # derived weight − fat when not entered
    smm_lb = Column(Float)             # skeletal muscle mass
    tbw_lb = Column(Float)             # total body water
    segments = Column(JSON)
    notes = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


class ClassBoard(Base):
    """One class-board read (note 91, 2026-10-29, alembic 0083): what the model read off
    the photo and the targets shown. Kept so targets can later be checked against what
    was actually lifted (Hevy), and so mis-reads can be counted. The photo is not stored.
    `items` = exercises with their match and `rec` as served; `picks` = the user's
    re-matches {n: title}, written when he changes one.
    """
    __tablename__ = "class_boards"

    id = Column(Integer, primary_key=True)
    date = Column(Date, nullable=False)          # local date of the class
    created_at = Column(DateTime, default=datetime.utcnow)
    source = Column(String)                      # "photo" | "text"
    model = Column(String)
    ms = Column(Integer)
    items = Column(JSON)
    unreadable = Column(JSON)
    picks = Column(JSON)
    easy = Column(Boolean, default=False)        # Easy day switch on (0084) — later: did it help?
    # Target vs what he logged in Hevy that day (0085; class_board.check_boards, after each
    # Hevy sync): [{idx, board_text, title, target_lb, actual_lb, actual_reps, verdict}].
    outcome = Column(JSON)
    checked_at = Column(DateTime)


class ClassAlias(Base):
    """Board wording → his Hevy title, learned (0085). From his "change" picks and from what
    he actually logged after a class (KB Sumo Squat High Pull → Kettlebell High Pull, 10-27).
    A learned alias overrides the model's match for the same wording on later boards."""
    __tablename__ = "class_aliases"

    id = Column(Integer, primary_key=True)
    key = Column(String, unique=True, nullable=False)   # class_board.alias_key(board wording)
    title = Column(String, nullable=False)
    source = Column(String)                             # "pick" | "log"
    seen = Column(Integer, default=1)
    updated_at = Column(DateTime, default=datetime.utcnow)


class CaffeineLog(Base):
    """One caffeine instance (a coffee, an espresso shot, a Starbucks drink…).

    Per-instance logging so dose AND timing are captured — the physiologically
    meaningful quantity is caffeine still ON BOARD at bedtime, a decay-weighted
    sum over every dose (services/caffeine.py). `mg` is the total for this entry
    (preset dose × quantity, or a custom value). Timestamp is naive UTC (app
    convention); the real time-of-day drives the pharmacokinetic calc.
    """
    __tablename__ = "caffeine_log"

    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, nullable=False)   # naive UTC — the actual dose time
    kind = Column(String, nullable=False)          # preset id or "custom"
    mg = Column(Float, nullable=False)             # total caffeine for this entry
    quantity = Column(Float, default=1.0)          # multiples of the preset
    entry_channel = Column(String)                 # 'voice' | NULL = app (0071)
    __table_args__ = (Index("ix_caffeine_log_ts", "timestamp"),)


class CaffeineDay(Base):
    """Per-day caffeine acknowledgement — how a day is marked COMPLETE without
    (or in addition to) instances. `ack` is 'none' (no caffeine today, a true
    zero) or 'done' (finished logging). Presence = the day is complete even with
    zero instances; absence + no instances + a past day = incomplete (excluded).
    """
    __tablename__ = "caffeine_day"

    date = Column(Date, primary_key=True)
    ack = Column(String, nullable=False)           # 'none' | 'done'


class DayFlag(Base):
    """A per-day 'this day is nonstandard, exclude it from analysis' marker.

    Generalises the legacy subjective_log.sick flag into named reasons so a day
    can be pulled from BOTH the correlation series and experiment eligibility
    (travel, illness, a bad night's environment, or auto-detected incomplete
    data). A day may carry several flags (e.g. travel + incomplete_data); any
    one flag excludes it. `source` distinguishes user-set from auto-derived
    (slept_away → travel; missing meals → incomplete_data), and the user can
    delete an auto flag to override a wrong auto-detection (e.g. the presence
    coords bug that once mislabelled home nights as travel).
    """
    __tablename__ = "day_flags"

    id = Column(Integer, primary_key=True)
    date = Column(Date, nullable=False)
    reason = Column(String, nullable=False)     # see DAY_FLAG_REASONS
    source = Column(String, default="manual")   # manual | auto
    note = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("date", "reason", name="uq_day_flag"),
        Index("ix_day_flags_date", "date"),
    )


class TripGap(Base):
    """What was NOT logged on a trip (note 102, 0090). Items in trip_gaps.ITEMS; empty =
    everything logged. Applies to the dates [start_date, end_date] as saved: a trip that is
    later extended needs its list saved again."""
    __tablename__ = "trip_gaps"

    id = Column(Integer, primary_key=True)
    start_date = Column(Date, nullable=False, unique=True)
    end_date = Column(Date, nullable=False)
    items = Column(JSON, nullable=False, default=list)
    updated_at = Column(DateTime, default=datetime.utcnow)


class SubjectiveLog(Base):
    __tablename__ = "subjective_log"

    id = Column(Integer, primary_key=True)
    date = Column(Date, unique=True, nullable=False)
    # Subscores are 1.0–5.0 floats since 0017 (0.1-step slider); legacy integer
    # entries are the same scale, just coarser. wellbeing_score is nullable:
    # an evening-only entry (day_score) can create the row without it.
    wellbeing_score = Column(Float)
    notes = Column(Text)
    sleep_quality = Column(Float)     # 1–5
    energy = Column(Float)            # 1–5 (5 = high energy)
    mood = Column(Float)              # 1–5
    soreness = Column(Text)           # free text description, null = none
    stress = Column(Float)            # 1–5 (5 = very stressed) — NOT in wellbeing_score avg
    sick = Column(Boolean)            # True = sick day; excluded from correlation series
    day_score = Column(Float)         # 1–5, evening rating of the day as a whole (lag-0 outcome)


class FoodLog(Base):
    __tablename__ = "food_log"

    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, nullable=False, default=datetime.utcnow)
    name = Column(String)                          # AI-generated short display name (2–5 words)
    # Optional brand / restaurant marker (0070, note 60). Part of the item's IDENTITY via
    # food_tags.item_key(name, description, brand) — never just display.
    brand = Column(String)
    description = Column(Text, nullable=False)
    meal_type = Column(String, default="snack")   # breakfast/lunch/dinner/dessert/snack/drinks
    calories = Column(Float)
    protein_g = Column(Float)
    carbs_g = Column(Float)
    fat_g = Column(Float)
    fiber_g = Column(Float)
    sugar_g = Column(Float)
    sodium_mg = Column(Float)
    portion_quantity = Column(Float)
    portion_unit = Column(String)
    # Parser self-reporting (both written from 2026-09-04; NULL means an older row,
    # not an unknown). `omitted_kcal` is what the parser deliberately did NOT count —
    # unmentioned condiments, sides, spreads — and is kept OUT of `calories` on
    # purpose so the exclusion stays visible instead of being silently folded in.
    confidence = Column(String)          # 'high' | 'medium' | 'low', re calorie total
    omitted_kcal = Column(Integer)
    llm_notes = Column(Text)   # Claude's stated assumptions
    # Set when this row was created BY a drink logger rather than typed by hand:
    # ('alcohol'|'caffeine', id of the source row). Deleting the drink deletes this.
    source_kind = Column(String)
    source_id = Column(Integer)
    # Alcohol entries only (0087): preset id or drink category; see services/fluids.
    drink_kind = Column(String)
    # Bought prepared from a restaurant, cafe, bar or food truck (note 84, 0091):
    # True / False; NULL = unknown. Set by the parser, editable in the tray.
    restaurant = Column(Boolean)
    is_saved = Column(Boolean, default=False)  # user-pinned for quick re-use
    skipped = Column(Boolean, default=False)   # intentionally-skipped meal (a KNOWN
                                               # zero, not missing data). Zero macros;
                                               # distinguishes "skipped lunch" from
                                               # "forgot to log lunch".
    # True when the clock time was filled in rather than given (2026-09-13, Alembic
    # 0062): a past-day entry with no time gets its meal's usual time. Meal-timing
    # analysis must drop these — the job the noon placeholder used to do.
    time_estimated = Column(Boolean, default=False)
    # My Foods link (Alembic 0063): which library food this row came from and how many
    # of its servings. The row's own macros stay the record — a snapshot at log time;
    # only an explicit apply-to-past rewrites them (docs/MY_FOODS.md).
    my_food_id = Column(Integer, index=True)
    entry_channel = Column(String)       # 'voice' | NULL = app (0071)
    servings = Column(Float)
    # Combination grouping (Alembic 0065, note 35): the parts of one logged combo share
    # a server-assigned combo_group; combo_part is "base" | "topping"; combo_name is the
    # combination's name (NULL for an ad-hoc base + topping). Presentation/provenance
    # only — each part's own macros remain the record.
    combo_group = Column(String, index=True)
    combo_part = Column(String)
    combo_name = Column(String)
    # Which household-app recipe this row came from (Alembic 0068, note 37). The macros
    # above stay the record — this is provenance, like my_food_id.
    recipe_id = Column(Integer, index=True)


class RecipeNutrition(Base):
    """Per-serving nutrition for a household-app recipe (Alembic 0068, note 37).

    The recipe itself (title, ingredients, steps, servings) lives in the household app's
    database and is read READ-ONLY — see services/recipes. This is only what wellness
    works out about it: the per-serving estimate, the servings it was divided by, and
    whether the user has checked the numbers. `source_hash` covers the recipe's ingredients
    and servings, so an edit over there marks this estimate stale.
    """
    __tablename__ = "recipe_nutrition"

    id = Column(Integer, primary_key=True)
    recipe_id = Column(Integer, nullable=False, unique=True, index=True)
    title = Column(String, nullable=False)
    servings = Column(Float)
    calories = Column(Float)
    protein_g = Column(Float)
    carbs_g = Column(Float)
    fat_g = Column(Float)
    fiber_g = Column(Float)
    sugar_g = Column(Float)
    sodium_mg = Column(Float)
    meal_type = Column(String)
    source = Column(String, nullable=False, default="estimate")   # estimate | confirmed
    source_hash = Column(String)
    # 0069: what the estimate assumed. total_json holds the WHOLE-recipe macros, so a
    # servings correction re-divides without another model call; servings_basis says
    # whether the recipe stated its yield or the model estimated it from the weight.
    total_json = Column(Text)
    total_weight_g = Column(Float)
    servings_basis = Column(String)          # stated | estimated | user
    serving_description = Column(String)
    assumptions = Column(Text)
    notes = Column(Text)
    hidden = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


class FoodParseCache(Base):
    """What was SAVED the last time a given text was typed (2026-09-13, Alembic 0062).

    69% of entries repeat an earlier one and about a third of repeats were retyped and
    re-parsed — a 3–6 s wait and drifting numbers for a food the app already knew.
    Keyed on the normalised typed text; holds the items as saved, so edits made before
    saving are what comes back. See routers/food_log._repeat_lookup.
    """
    __tablename__ = "food_parse_cache"

    id = Column(Integer, primary_key=True)
    text_key = Column(String, nullable=False, unique=True, index=True)
    items_json = Column(Text, nullable=False)
    hits = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, default=datetime.utcnow)


class MyFood(Base):
    """Personal food library — the foods worth precise facts (docs/MY_FOODS.md, 0063).

    Macros are PER SERVING; carbs are TOTAL (fiber included). `units` is a JSON list of
    alternate units as servings-per-unit: [{"label": "slice", "factor": 0.125}].
    `item_key` is food_tags.item_key(name, None, brand), so food-group tags keep resolving.
    """
    __tablename__ = "my_foods"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    brand = Column(String)          # 0070; item_key = food_tags.item_key(name, None, brand)
    item_key = Column(String, nullable=False, unique=True, index=True)
    role = Column(String, nullable=False, default="food")          # food | topping
    meal_type = Column(String)
    serving_label = Column(String)
    serving_qty = Column(Float, nullable=False, default=1.0)
    serving_unit = Column(String, nullable=False, default="serving")
    units = Column(Text)
    calories = Column(Float, nullable=False)
    protein_g = Column(Float)
    carbs_g = Column(Float)
    fat_g = Column(Float)
    fiber_g = Column(Float)
    sugar_g = Column(Float)
    sodium_mg = Column(Float)
    source = Column(String, nullable=False, default="confirmed")   # label | confirmed | estimate
    notes = Column(Text)
    archived = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)
    # 0064: extra item keys this food answers to (merged name variants), and for
    # role='combo' the parts [{"my_food_id": int, "servings": float}] — a combo's
    # macros are the sum of its parts, kept in sync when a part changes.
    aliases = Column(Text)
    components = Column(Text)
    # 0067 (note 34): named drinks. drink_kind is one of routers/my_foods.DRINK_KINDS;
    # abv > 0 makes it alcoholic, and then serving_unit must be "oz" so standard drinks
    # follow from serving_qty x servings x abv (services/intake.sdu_from).
    drink_kind = Column(String)
    abv = Column(Float)


class MyFoodDismissal(Base):
    """A seeding candidate the user decided is not worth a My Food (0064)."""
    __tablename__ = "my_food_dismissals"

    id = Column(Integer, primary_key=True)
    item_key = Column(String, nullable=False, unique=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class MyFoodBackfill(Base):
    """Prior values of every row an apply-to-past rewrote — the undo record."""
    __tablename__ = "my_food_backfills"

    id = Column(Integer, primary_key=True)
    my_food_id = Column(Integer, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    n_rows = Column(Integer, nullable=False, default=0)
    rows_json = Column(Text, nullable=False)
    undone_at = Column(DateTime)


class UiView(Base):
    """One row per screen view (2026-09-13) — see routers/telemetry."""
    __tablename__ = "ui_views"

    id = Column(Integer, primary_key=True)
    view = Column(String, nullable=False, index=True)
    ts = Column(DateTime, nullable=False, default=datetime.utcnow)


class FoodItemTag(Base):
    """Food-group tags keyed by normalized item name — see migration 0055.

    Attached to the ITEM rather than the log row so that reusing a saved food
    inherits its composition. Keyword-matching entry TEXT measured the logging
    channel instead of the food; this does not.
    """
    __tablename__ = "food_item_tags"

    id = Column(Integer, primary_key=True)
    item_key = Column(String, nullable=False, unique=True, index=True)
    display_name = Column(String)
    primary_cat = Column(String)     # one dominant category (retained; nothing reads it now)
    groups = Column(Text)            # JSON array of group slugs (multi-label)
    components = Column(Text)        # JSON {component: calorie share %} summing to 100.
                                     # NULL = not yet decomposed → eligible for tagging.
    source = Column(String, nullable=False, default="llm")   # llm | manual
    model = Column(String)
    failed = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class UserSettings(Base):
    __tablename__ = "user_settings"

    id = Column(Integer, primary_key=True)
    target_sleep_hours = Column(Float, default=8.0)
    max_hr = Column(Integer, default=185)
    resting_hr_override = Column(Integer)
    # FROZEN resting-HR reference for %HRR zone boundaries (services/hr_zones.py).
    # Deliberately NOT the rolling rhr_baseline: deriving zone edges from a drifting
    # baseline moves the zone definitions under the data and breaks longitudinal
    # comparability. Set once from the observed baseline, changed only on purpose.
    hrr_rest_ref = Column(Integer)
    training_goal = Column(String, default="building")
    steps_goal = Column(Integer, default=10000)
    move_goal = Column(Integer, default=500)
    exercise_goal = Column(Integer, default=30)
    ai_enabled = Column(Boolean, default=True)
    protein_goal_g = Column(Integer)   # nullable — None means auto-calc from weight + training_goal
    # nullable — None means auto-calc at 14 g per 1,000 kcal (the DGA/IOM figure),
    # which scales the target with intake instead of pinning a flat number that's
    # wrong at both ends of a cut.
    fiber_goal_g = Column(Integer)
    calorie_goal = Column(Integer)     # nullable — None means no calorie target set (fixed mode)
    # "fixed" = use calorie_goal number; "maintain" = goal tracks adaptive TDEE (show over/under)
    # "fixed" = use calorie_goal number; "balance" = goal = daily Out (RMR + active);
    # "pace" = Out + daily_offset from weight_change_goal_lbs_per_week; "maintain" = legacy alias for "balance"
    calorie_goal_mode = Column(String, default="fixed")
    weight_change_goal_lbs_per_week = Column(Float)  # nullable; negative = loss, positive = gain (pace mode)
    # Physical profile — used for RMR / energy balance estimation
    height_cm = Column(Float)          # nullable
    birth_year = Column(Integer)       # nullable
    biological_sex = Column(String)    # 'male' | 'female' | nullable
    last_webhook_at = Column(DateTime) # updated each time webhook delivers records
    last_morning_pipeline_date = Column(Date)  # once-per-day guard for the morning pipeline
    caffeine_cutoff_hour = Column(Integer, default=14)  # hour of day (24h), default 2pm
    light_cutoff_hour    = Column(Integer, default=10)  # hour of day (24h), default 10am
    # User's caffeine preset list [{id,label,emoji,mg}], overriding the hardcoded
    # defaults so mg values can match what they actually drink. None = defaults.
    caffeine_presets     = Column(JSON)
    alcohol_presets      = Column(JSON)   # null = defaults (services/intake.ALCOHOL_PRESETS)
    water_bottle_oz      = Column(Integer, default=30)   # seeds the default water preset
    water_presets        = Column(JSON)   # null = defaults (services/intake.WATER_PRESETS)


class HabitLog(Base):
    """One row per calendar day. All fields True = did the positive behaviour."""
    __tablename__ = "habit_logs"

    date             = Column(Date, primary_key=True)
    no_late_caffeine = Column(Boolean)  # no caffeine after 2 pm
    outdoor_light_am = Column(Boolean)  # outdoor light before 10 am
    vitamin_d        = Column(Boolean)  # legacy — superseded by supplements
    magnesium        = Column(Boolean)  # legacy — superseded by supplements
    supplements      = Column(Boolean)  # took vitamins / supplements
    creatine         = Column(Boolean)  # took creatine
    meditation       = Column(Boolean)  # meditated
    # Evening-check-in toggles (True = the behaviour HAPPENED — negative behaviours,
    # unlike the positive habits above; polarity handled in INPUTS labels)
    ate_late         = Column(Boolean)  # DEAD 2026-07-30 — removed from UI + correlations
                                        # (zero-variance for this user's fixed routine).
                                        # Column kept for history (legacy-column convention).
    # Optional continuous companions (minutes since LOCAL midnight, 0–1439). When
    # set they feed continuous correlation inputs; caffeine time also DERIVES
    # no_late_caffeine. Null = not logged (the boolean toggle stands on its own).
    caffeine_last_min = Column(Integer)  # time of last caffeine
    meditation_min    = Column(Integer)  # time meditated
    screens_in_bed   = Column(Boolean)  # phone/screens in bed before sleep
    notes            = Column(String)


class HabitGoal(Base):
    """A weekly/monthly frequency goal for an Activity habit (Phase 2)."""
    __tablename__ = "habit_goals"

    habit_key = Column(String, primary_key=True)  # e.g. "worked_out"
    target    = Column(Integer, nullable=False)   # times per period
    period    = Column(String, nullable=False)    # 'week' | 'month'


class WeeklySummary(Base):
    __tablename__ = "weekly_summaries"

    id = Column(Integer, primary_key=True)
    week_start = Column(Date, unique=True, nullable=False)  # Monday
    week_end = Column(Date, nullable=False)                  # Sunday
    summary_text = Column(Text)
    model = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


class MonthlySummary(Base):
    __tablename__ = "monthly_summaries"

    id = Column(Integer, primary_key=True)
    year_month = Column(String, unique=True, nullable=False)  # "YYYY-MM"
    month_start = Column(Date, nullable=False)
    month_end = Column(Date, nullable=False)
    summary_text = Column(Text)
    model = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


class InsightCache(Base):
    """Cached cross-metric correlation results, recomputed weekly."""
    __tablename__ = "insight_cache"

    id = Column(Integer, primary_key=True)
    metric_a = Column(String, nullable=False)   # e.g. "sleep_total_min"
    metric_b = Column(String, nullable=False)   # e.g. "readiness_score"
    lag_days = Column(Integer, default=0)       # metric_b is lag_days after metric_a
    pearson_r = Column(Float)                   # Pearson r (reference; not headline)
    spearman_r = Column(Float)                  # Spearman ρs (headline coefficient)
    n = Column(Integer)                         # number of paired observations
    n_eff = Column(Integer)                     # autocorrelation-adjusted effective N
    p_value = Column(Float)                     # raw p-value (before BH correction, from Spearman)
    ci_low = Column(Float)                      # Fisher z 95% CI lower bound (Spearman)
    ci_high = Column(Float)                     # Fisher z 95% CI upper bound (Spearman)
    days_window = Column(Integer, default=90)   # lookback window used
    # Same pair over the PREVIOUS window (year-over-year). Not a second estimate to
    # average in — a drift check: agreement means the relationship is stable, and
    # disagreement is itself the finding.
    prev_spearman_r = Column(Float)
    prev_n = Column(Integer)
    # Reverse-lag placebo (2026-10-27, 0079): the same input against the outcome measured
    # BEFORE it (lag reverse_lag_days < 0). fwd_rev_* = sign(ρ)·(ρ_fwd − ρ_rev) with a
    # block-bootstrap 95% interval, survivors only; see correlations.REVERSE_BOOT_REPS.
    reverse_lag_days = Column(Integer)
    reverse_spearman_r = Column(Float)
    reverse_n = Column(Integer)
    reverse_ci_low = Column(Float)
    reverse_ci_high = Column(Float)
    fwd_rev_diff = Column(Float)
    fwd_rev_diff_low = Column(Float)
    fwd_rev_diff_high = Column(Float)
    forward_exceeds_reverse = Column(Boolean)
    # Why the pair has no p-value (0080): by_design / few_pairs / no_variation_input /
    # no_variation_outcome / low_neff — see correlations.untestable_reason.
    untestable_reason = Column(String)
    # Effect in natural units, observed range, influential days (0081) — survivors only;
    # see correlations.survivor_details.
    survivor_details = Column(JSON)
    computed_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("metric_a", "metric_b", "lag_days", name="uq_insight_cache"),
    )


class FindingLedger(Base):
    """Pre-registration ledger for correlation findings: the date each pair FIRST
    crossed an evidence tier (suggestive / confirmed), with ρ and n at that moment.
    Never overwritten once set — the prospective-validation clock starts here, so
    all data after the crossing is untouched by the selection that produced the
    finding (see correlations.build_validation)."""
    __tablename__ = "finding_ledger"

    id = Column(Integer, primary_key=True)
    metric_a = Column(String, nullable=False)
    metric_b = Column(String, nullable=False)
    lag_days = Column(Integer, default=0)
    first_suggestive_at = Column(Date)
    rho_at_suggestive = Column(Float)
    n_at_suggestive = Column(Integer)
    first_confirmed_at = Column(Date)
    rho_at_confirmed = Column(Float)
    n_at_confirmed = Column(Integer)
    # Which window measured this entry. Pre-registration only means something if the
    # ruler is recorded: entries made before 2026-08-17 used a 180-day window, later
    # ones use 365, and comparing them without knowing which is meaningless.
    days_window = Column(Integer)

    __table_args__ = (
        UniqueConstraint("metric_a", "metric_b", "lag_days", name="uq_finding_ledger"),
    )


class BloodMarker(Base):
    """One lab-panel marker at one draw date (flat rows — trends fall out of a
    GROUP BY marker). Canonical `marker` keys let the same analyte match across
    labs with different display names; `label` keeps the lab's own wording."""
    __tablename__ = "blood_markers"

    id = Column(Integer, primary_key=True)
    date = Column(Date, nullable=False)        # draw date
    marker = Column(String, nullable=False)    # canonical key, e.g. "apob"
    label = Column(String)                     # lab's display name
    value = Column(Float, nullable=False)
    unit = Column(String)
    ref_low = Column(Float)                    # lab reference range, if given
    ref_high = Column(Float)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("date", "marker", name="uq_blood_marker_day"),
    )


class BloodMarkerTarget(Base):
    """A personal target for one marker, overriding the seeded default.

    Separate from the lab's reference range on purpose: a reference range is the
    population interval the lab measured, a target is what you're aiming at. Only
    OVERRIDDEN markers get a row — the defaults live in services/blood_markers and
    are not copied in, so improving a default benefits every marker you haven't
    deliberately pinned. A row with both bounds NULL means "no target", which is how
    you switch one off without losing that you made that choice.
    """
    __tablename__ = "blood_marker_targets"

    marker = Column(String, primary_key=True)     # canonical key, e.g. "apob"
    target_low = Column(Float)
    target_high = Column(Float)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Experiment(Base):
    """A randomized n-of-1 experiment: on eligible days a coin assigns arm_a or
    arm_b, shown as the coach's recommendation. Assignment ≠ behavior — compliance
    is auto-detected from workout records at analysis time, and the primary
    analysis is intent-to-treat (assignment → outcome), which the randomization
    keeps valid regardless of compliance."""
    __tablename__ = "experiments"

    id = Column(Integer, primary_key=True)
    label = Column(String, nullable=False)          # e.g. "HIIT vs rest → next-morning energy"
    arm_a = Column(String, nullable=False)          # e.g. "hiit"
    arm_b = Column(String, nullable=False)          # e.g. "rest"
    # "training" | anything else (e.g. "supplement"). Drives TWO things:
    #   1. eligibility rules — the training rules (REST-day rec, consecutive-training
    #      cap) are meaningless for an intervention you simply take, and applying them
    #      would discard ~half the days for nothing. See coach.check_eligibility.
    #   2. whether the arm becomes the coach's daily training recommendation. Arms are
    #      mapped through _ARM_TO_LABEL into workout labels, so only a training
    #      experiment may surface there — otherwise a supplement arm would render as a
    #      workout. Only one training experiment runs at a time; other kinds may run
    #      concurrently, which is free power (independent randomisation = orthogonal).
    kind = Column(String, nullable=False, server_default="training")
    primary_outcome = Column(String, default="subj_energy")
    # Accrual target for THIS trial, from its own pre-registration — not a global.
    # 30/arm suits a d~0.5 question; L-theanine registered ~350 eligible days (175/arm)
    # for d~0.3 and must not be shown, or read, against 30.
    target_per_arm = Column(Integer, nullable=False, server_default="30")
    started_at = Column(Date)
    ended_at = Column(Date)                         # null = active
    notes = Column(Text)


class ExperimentAssignment(Base):
    """One randomized day. Snapshot columns record the recovery state at
    assignment time (provenance for the eligibility rule)."""
    __tablename__ = "experiment_assignments"

    id = Column(Integer, primary_key=True)
    experiment_id = Column(Integer, nullable=False)
    date = Column(Date, nullable=False)
    arm = Column(String, nullable=False)
    readiness_at_assignment = Column(Float)
    hrv_z_at_assignment = Column(Float)
    # Arms are drawn the night before (blind to eligibility — the draw is
    # independent of it); the morning pipeline confirms. Only confirmed rows
    # enter the ITT analysis. None = pending confirmation.
    eligible_confirmed = Column(Boolean)
    ineligible_reason = Column(String)
    # Self-reported compliance. Needed because _behavior_by_day auto-detects compliance
    # from WORKOUT records, which says nothing about whether a capsule was swallowed.
    # ITT (by assignment) remains the primary analysis and stays valid regardless — this
    # is for per-protocol context and, more importantly, for spotting protocol DRIFT.
    complied = Column(Boolean)
    # Minutes since local midnight. For an acute intervention the dose-to-outcome
    # interval must be a constant: L-theanine's half-life is ~1 h, so a morning dose is
    # ~12 half-lives from bedtime and cannot affect that night's HRV — it is a placebo
    # day mislabelled as treatment. Since required n scales as 1/d², diluting the effect
    # by dosing at the wrong time multiplies the duration quadratically. Recording the
    # time does NOT repair that (conditioning on a post-randomisation variable breaks
    # the comparison); it exists to make the drift visible while it can still be fixed.
    dose_minute = Column(Integer)

    __table_args__ = (
        UniqueConstraint("experiment_id", "date", name="uq_experiment_assignment_day"),
    )


class Supplement(Base):
    """One supplement in the regimen, with the dates it was actually being taken.

    `HabitLog.supplements` is a single boolean — "took today's stack" — which cannot say
    WHAT the stack was. That's a real limitation for the correlation engine: the
    `habit_supplements` input covers several compounds at once, so it can never attribute
    an effect to any one of them. This table records the composition of the regimen over
    time, so "what was I taking on 2026-03-14" is answerable retrospectively.

    Starting or stopping one writes an `Intervention` at the same date (see
    routers/supplements.py). That's what makes before/after visible at all — without a
    dated record, a change six months ago is simply invisible to every chart.

    `ended_on` NULL = currently taking. Rows are never deleted on stop: the history is
    the point, and a deleted row would silently rewrite what the regimen was.
    """
    __tablename__ = "supplements"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)       # "L-theanine", "Magnesium glycinate"
    dose = Column(Float)                        # amount per day, null = unspecified
    unit = Column(String)                       # "mg", "g", "IU", "capsule"
    timing = Column(String)                     # "am" | "pm" | "with_food" | "as_needed"
    started_on = Column(Date, nullable=False)
    ended_on = Column(Date)                     # null = still taking
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (Index("ix_supplements_started_on", "started_on"),)


class MetricAck(Base):
    """A "yes, this silence is real" acknowledgement for one stalled input.

    Deliberately a SNOOZE, not a mute: `acked_until` is set one of the metric's
    own expected gaps into the future, so a feed that stays silent asks again
    rather than disappearing from view. A permanent mute would convert the one
    case a person confirmed as normal into a blind spot for the case where the
    pipeline genuinely breaks — see services/metric_freshness.
    """
    __tablename__ = "metric_ack"

    metric = Column(String, primary_key=True)   # health_metrics type, or a stream key
    acked_at = Column(DateTime, default=datetime.utcnow)
    acked_until = Column(Date, nullable=False)  # silence is expected through this date
    note = Column(String)                       # why — e.g. "genuinely none lately"


class Intervention(Base):
    """A user-flagged behavioral change, for interrupted-time-series (before/after) analysis."""
    __tablename__ = "interventions"

    id = Column(Integer, primary_key=True)
    date = Column(Date, nullable=False)       # the day the change took effect
    label = Column(String, nullable=False)    # e.g. "creatine + higher volume"
    notes = Column(String)
    # supplement | training | diet | sleep | other; NULL = uncategorised, drawn as other
    # (Alembic 0066, notes 32/33 — sets the chart marker glyph).
    category = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


class TvSession(Base):
    """TV viewing from Home Assistant, kept here because HA holds ~10 days (note 85, 0088).
    `segments`: [[start, end, app], ...] naive-UTC ISO; `mine`: NULL = not answered."""
    __tablename__ = "tv_sessions"

    id = Column(Integer, primary_key=True)
    start_time = Column(DateTime, unique=True, nullable=False)
    end_time = Column(DateTime, nullable=False)
    date = Column(Date, nullable=False, index=True)    # local date it started
    minutes = Column(Float, nullable=False)            # active time, without gaps
    segments = Column(JSON, nullable=False)
    mine = Column(Boolean)                             # watcher == "me"
    watcher = Column(String)                           # me | family | other (0089)
    answered_at = Column(DateTime)


class SwitchStep(Base):
    """A reading of the Switch's minutes-today counter when it changed (0089). It reports in
    chunks during play, so these date a session to ~5-10 min; HA keeps them only ~10 days."""
    __tablename__ = "switch_steps"

    id = Column(Integer, primary_key=True)
    time = Column(DateTime, unique=True, nullable=False)   # naive UTC
    minutes = Column(Float, nullable=False)


class HealthEvent(Base):
    """A vaccine, procedure or medication change (note 93, 2026-10-31, alembic 0086).

    Not an Intervention (those are lasting changes, and before/after analysis would read a
    flu shot as a step) and not a DayFlag (those drop days from baselines and correlations;
    he was not sick). See services/health_events.py.
    """
    __tablename__ = "health_events"

    id = Column(Integer, primary_key=True)
    date = Column(Date, nullable=False, index=True)
    kind = Column(String, nullable=False)     # vaccine | procedure | medication | other
    name = Column(String, nullable=False)     # e.g. "Flu shot"
    notes = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


class HevyWorkout(Base):
    """A strength session synced from the Hevy API. Set-level detail lives in hevy_sets;
    the linked Apple Health session (HR/TRIMP) is workouts.apple_uuid via time overlap."""
    __tablename__ = "hevy_workouts"

    id = Column(String, primary_key=True)          # Hevy workout UUID
    title = Column(String)
    start_time = Column(DateTime, nullable=False)  # naive UTC (matches workouts convention)
    end_time = Column(DateTime)
    hevy_updated_at = Column(DateTime)             # Hevy's updated_at
    apple_uuid = Column(String)                    # workouts.apple_uuid of the overlapping Apple session
    tonnage_kg = Column(Float)                     # Σ weight×reps over non-warmup sets
    working_sets = Column(Integer)                 # count of non-warmup sets
    avg_top_set_rpe = Column(Float)                # mean RPE across each exercise's top set
    synced_at = Column(DateTime, default=datetime.utcnow)
    pr_notified_at = Column(DateTime)              # set once a PR push fired — dedupes re-syncs

    __table_args__ = (Index("ix_hevy_workouts_start", "start_time"),)


class HevySet(Base):
    __tablename__ = "hevy_sets"

    id = Column(Integer, primary_key=True)
    hevy_workout_id = Column(String, nullable=False)
    exercise_index = Column(Integer)
    exercise_title = Column(String)     # denormalized — survives template renames
    exercise_template_id = Column(String)
    superset_id = Column(Integer)
    set_index = Column(Integer)
    set_type = Column(String)           # normal / warmup / dropset / failure
    weight_kg = Column(Float)
    reps = Column(Integer)
    distance_meters = Column(Float)
    duration_seconds = Column(Integer)
    rpe = Column(Float)

    __table_args__ = (
        Index("ix_hevy_sets_workout", "hevy_workout_id"),
        Index("ix_hevy_sets_template", "exercise_template_id"),
    )


class HevyExerciseTemplate(Base):
    """Cache of Hevy exercise templates — provides muscle-group attribution for volume."""
    __tablename__ = "hevy_exercise_templates"

    id = Column(String, primary_key=True)
    title = Column(String)
    exercise_type = Column(String)              # e.g. weight_reps, bodyweight_reps, duration
    primary_muscle_group = Column(String)
    secondary_muscle_groups = Column(JSON)      # list of strings
    is_custom = Column(Boolean)
    fetched_at = Column(DateTime, default=datetime.utcnow)


class HevySyncState(Base):
    """Single-row sync cursor for the Hevy events endpoint (pattern: UserSettings)."""
    __tablename__ = "hevy_sync_state"

    id = Column(Integer, primary_key=True)
    last_event_check = Column(DateTime)   # `since` cursor for /v1/workouts/events (naive UTC)
    backfill_done = Column(Boolean, default=False)
    last_sync_at = Column(DateTime)
    last_error = Column(String)


class ApiCallLog(Base):
    __tablename__ = "api_call_log"

    id = Column(Integer, primary_key=True)
    feature = Column(String)
    model = Column(String)
    input_tokens = Column(Integer)
    output_tokens = Column(Integer)
    cost_usd = Column(Float)
    created_at = Column(DateTime, default=datetime.utcnow)


class WeatherDaily(Base):
    """One row per LOCAL day of outdoor conditions (Open-Meteo).

    Stored daily — not just displayed — so weather/air quality can become
    correlation inputs (does poor AQI or a pressure swing track worse sleep,
    respiratory rate, mood?). Aggregates come from the day's hourly series, so a
    row is forecast-inclusive early in the day and converges to actuals as the
    hourly refresh job re-upserts it. Source is deliberately swappable — a local
    HA AQI sensor can overwrite `aqi_*` later without touching the schema.
    """
    __tablename__ = "weather_daily"

    date = Column(Date, primary_key=True)      # local calendar day
    temp_mean = Column(Float)                  # °F
    temp_min = Column(Float)
    temp_max = Column(Float)
    apparent_max = Column(Float)               # "feels like" high
    humidity_mean = Column(Float)              # %
    pressure_mean = Column(Float)              # hPa (mean sea level)
    pressure_min = Column(Float)
    pressure_delta = Column(Float)             # max−min within the day (barometric swing)
    uv_max = Column(Float)
    precip_sum = Column(Float)
    aqi_mean = Column(Float)                   # US AQI — authoritative daily value
    aqi_max = Column(Float)
    aqi_source = Column(String)                # airnow-official | airnow | open-meteo
    aqi_pollutant = Column(String)             # driving sub-index (PM2.5/PM10/OZONE)
    aqi_samples = Column(Integer, default=0)   # hourly observations folded into aqi_mean
    aqi_last_obs = Column(String)              # dedupe key so re-runs don't double-count
    pm25_mean = Column(Float)
    condition = Column(String)                 # human label from WMO weather_code
    sunrise = Column(DateTime)                 # naive UTC (app convention), converted from local
    sunset = Column(DateTime)                  # naive UTC
    updated_at = Column(DateTime, default=datetime.utcnow)


class ScreentimeEvent(Base):
    """One app-focus segment from the Mac (knowledgeC) or iPhone (Biome).

    Times are stored **naive UTC** (like every other timestamp in this DB) so
    `local_date_sql` buckets them into the correct local day. The collector
    sends offset-aware ISO strings; the ingest endpoint converts to naive UTC
    before writing. `category` is derived server-side from `app` at ingest
    (see screentime_categories.py). The uniqueness key makes re-POSTing the
    same window idempotent.
    """
    __tablename__ = "screentime_events"

    id = Column(Integer, primary_key=True)
    app = Column(String, nullable=False)              # bundle id, e.g. com.instagram.Instagram
    duration_sec = Column(Integer, nullable=False)
    start_time = Column(DateTime, nullable=False)     # naive UTC
    end_time = Column(DateTime)                        # naive UTC
    device = Column(String, nullable=False)           # "iPhone" | "Mac"
    category = Column(String)                          # derived server-side
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("app", "start_time", "device", name="uq_screentime_app_start_device"),
        Index("ix_screentime_start", "start_time"),
    )


class GameStatDaily(Base):
    """Per-day XP/level for one gamification stat — a GAME-OWNED table.

    THE INVARIANT (docs/GAMIFICATION.md): the game layer is a read-only mirror. It
    may write here and nowhere else, and NO analysis path (correlations, experiments,
    coach, metrics) may ever read this table back. If that rule holds, the
    measurement instrument stays honest regardless of what the game does.

    Fully derived — safe to DELETE and recompute from source at any time
    (services/game.recompute_and_store, run daily by the morning pipeline). Rows are
    persisted rather than computed on request because level-UP detection needs
    yesterday's level, and because banding ~1.5 years x 6 stats on every tab load
    would be slow.
    """
    __tablename__ = "game_stat_daily"

    date  = Column(Date, primary_key=True)
    stat  = Column(String, primary_key=True)   # see services/game.STATS
    raw   = Column(Float)    # the underlying metric that day (tonnage, CTL, HRV, ...)
    score = Column(Float)    # performance vs your own rolling norm, 0..2 (1.0 = at norm)
    xp    = Column(Float)    # XP earned that day = BASE_XP_PER_DAY * score
    cum_xp = Column(Float)   # cumulative XP through that day (monotone non-decreasing)
    level = Column(Integer)  # level implied by cum_xp
    # Per-component breakdown for that day: [{key,label,weight,score,raw,detail}].
    # Powers the "how is this computed" drill-down and the lever ranking
    # (services/game.levers). Derived like everything else here.
    components = Column(JSON)

    __table_args__ = (Index("ix_game_stat_daily_stat_date", "stat", "date"),)


class PvtSession(Base):
    """One Psychomotor Vigilance Task run.

    PVT is the standard sleep-research vigilance instrument: a stimulus appears after
    a random delay, you respond as fast as possible, repeat. Its value here is that it
    is essentially immune to practice effects (unlike most cognitive tests), so daily
    numbers stay comparable over years — which is what a longitudinal n-of-1 needs.

    RAW TRIALS ARE STORED and every summary metric is derived server-side
    (services/pvt.py), because the literature disagrees on the brief-version
    parameters: Basner 2011 (PVT-B) uses a 355 ms lapse threshold with a 2-5 s ISI,
    while common implementations use 500 ms / 1-4 s. Keeping the trials means the
    threshold is a reporting choice, not a collection choice — both are computed, and
    any future metric can be back-derived without re-collecting data.

    `local_minute` is stored explicitly (not just derived from started_at) because PVT
    performance has a strong circadian rhythm: without controlling for time of day,
    circadian variation swamps the sleep signal. It is the covariate that lets the
    real effect be separated from the time-of-day effect.
    """
    __tablename__ = "pvt_session"

    id = Column(Integer, primary_key=True)
    date = Column(Date, nullable=False)             # local calendar day
    started_at = Column(DateTime, nullable=False)   # naive UTC (app convention)
    local_minute = Column(Integer, nullable=False)  # minutes since local midnight
    duration_s = Column(Float)                      # actual elapsed test time

    n_trials = Column(Integer)
    median_rt = Column(Float)          # ms
    mean_rt = Column(Float)            # ms
    mean_reciprocal_rt = Column(Float) # 1000/RT averaged — the standard transform;
                                       # more normally distributed than raw RT
    slowest10_mean = Column(Float)     # mean of the slowest 10% (lapse-domain measure)
    fastest10_mean = Column(Float)     # mean of the fastest 10% (optimum-response measure)
    lapses_355 = Column(Integer)       # Basner PVT-B threshold
    lapses_500 = Column(Integer)       # classic 10-min PVT threshold
    false_starts = Column(Integer)     # responses before the stimulus (or < 100 ms)
    invalid_trials = Column(Integer)   # discarded as implausible. Nonzero means the
                                       # CLIENT clock misbehaved — distrust the whole
                                       # session, don't score the remainder.

    # ── added with protocol v2 (2026-08-12) ──────────────────────────────────────
    # Collection parameters changed, so scores are only comparable WITHIN a protocol.
    # v1 used a ~3-6 s response-to-stimulus interval (a misreading of Basner 2011),
    # yielding ~37 trials per 3 min; v2 uses the specified 1-4 s (feedback inclusive)
    # over 2 min for ~42. Always segment by this in trends and correlations — the
    # duration differs too, so the metrics carry different precision. Never pool.
    protocol = Column(Integer, nullable=False, server_default="1")

    timeouts = Column(Integer)         # v2: trials with no response before the client
                                       # timeout, kept and scored at the timeout value
                                       # (as Basner does). v1 discarded these, throwing
                                       # away the most diagnostic events the PVT has.
    timing_suspect = Column(Boolean, default=False)  # structural timeline check failed
    timing_problems = Column(JSON)     # first few human-readable integrity failures

    # Basner's composite and the time-on-task slope. Both are AGGREGATE-ONLY: measured
    # within-session CV is 7.1% for the score, and the slope's |t| is 0.3-0.5 on rested
    # sessions (needs >2 to claim a trend). Stored because they pool usefully over
    # weeks — do not surface either as a single-session headline.
    perf_score = Column(Float)         # 100% − (lapses+false starts)/(valid+false starts)
    tot_slope = Column(Float)          # OLS slope of 1/RT vs trial index, per 10 trials
    slowest10_speed = Column(Float)    # slowest decile as 1/RT — the form Basner reports,
                                       # and the one that survives a timeout in the decile
    interrupted = Column(Boolean, default=False)  # tab was backgrounded mid-test

    trials = Column(JSON)              # v2: [{"isi_ms", "blank_ms", "rt_ms", "t_stim_ms",
                                       #       "t_resp_ms", "false_start", "timed_out"}]
    device = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (Index("ix_pvt_session_date", "date"),)


class KssLog(Base):
    """One Karolinska Sleepiness Scale rating — 9-point subjective sleepiness.

    Deliberately NOT part of `subjective_log`. That table is one row per DATE on a 1–5
    scale; KSS is a 9-point STATE measure referring to the last ~10 minutes, and its value
    comes from being paired with the objective test taken at the same sitting. The finding
    worth catching is the DISSOCIATION — measurably impaired on the PVT while reporting
    feeling fine — which is only visible if both are timestamped to the same moment.

    Low = alert, high = sleepy. Note this is the opposite polarity to `subjective_log.energy`,
    so it must be registered with higher_is_better=False wherever it reaches the correlation
    engine.
    """
    __tablename__ = "kss_log"

    id = Column(Integer, primary_key=True)
    date = Column(Date, nullable=False)
    started_at = Column(DateTime, nullable=False)   # naive UTC (app convention)
    local_minute = Column(Integer, nullable=False)  # circadian covariate, as for PVT
    score = Column(Integer, nullable=False)         # 1 = extremely alert … 9 = fighting sleep
    device = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (Index("ix_kss_log_date", "date"),)


class TappingSession(Base):
    """One maximum-rate finger-tapping block.

    Role in the battery is as a CONTROL for the PVT, not a headline. Sleep loss slows PVT
    responses — but so do motor and peripheral factors. If PVT slows and tapping doesn't,
    the slowing is attentional; if both slow, it isn't specific to vigilance.

    Read `iti_cv` first: motor VARIABILITY is the fatigue-sensitive measure, while raw
    tapping rate is comparatively robust. Practice effects are minimal and plateau after
    one or two sessions, which is what makes it viable daily alongside PVT.
    """
    __tablename__ = "tapping_session"

    id = Column(Integer, primary_key=True)
    date = Column(Date, nullable=False)
    started_at = Column(DateTime, nullable=False)
    local_minute = Column(Integer, nullable=False)
    protocol = Column(Integer, nullable=False, server_default="1")
    duration_ms = Column(Integer)
    hand = Column(String)              # keep this CONSTANT across sessions or the series is noise

    n_taps = Column(Integer)
    tap_rate = Column(Float)           # taps/s across the whole block
    mean_iti = Column(Float)           # ms between taps
    iti_sd = Column(Float)
    iti_cv = Column(Float)             # percent — the headline metric
    first_half_rate = Column(Float)    # split by TIME, not by tap index
    second_half_rate = Column(Float)
    rate_decrement_pct = Column(Float) # negative = slowed across the block; aggregate-only
    artifacts = Column(Integer)        # sub-threshold intervals = double-registrations

    taps = Column(JSON)                # raw offsets from block start, ms
    device = Column(String)
    interrupted = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (Index("ix_tapping_session_date", "date"),)


class EnergyCalibration(Base):
    """
    History of fitted personal activity factors (k).

    Kept as an append-only series rather than a single mutable value so the fit can be
    inspected over time — a moving k is itself a signal (metabolic adaptation, or a
    change in logging behaviour), and it's the counterpart to the parser-drift canary.
    """
    __tablename__ = "energy_calibration"

    id = Column(Integer, primary_key=True)
    fitted_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    k = Column(Float, nullable=False)      # applied (clamped) factor
    k_raw = Column(Float)                  # pre-clamp, so a rail-hitting fit stays visible
    k_ci_low = Column(Float)
    k_ci_high = Column(Float)

    rmr = Column(Float)                    # RMR the fit was conditioned on
    rmr_basis = Column(String)             # "katch_mcardle" | "mifflin"
    mean_active = Column(Float)            # mean Apple active kcal over the window
    adaptive_tdee = Column(Float)          # weight-calibrated TDEE the fit solved against
    n_intake_days = Column(Integer)
    window_days = Column(Integer)

    __table_args__ = (Index("ix_energy_calibration_fitted", "fitted_at"),)


class AppNote(Base):
    """A quick note captured in the app — a feature idea or a bug seen in passing.

    Read this table at the start of a working session: it is where requests land
    between sessions, and an idea nobody reads is the same as one never written.
    Marking `status='done'` keeps the note rather than deleting it, so the record of
    what was asked for survives.
    """
    __tablename__ = "app_notes"

    id = Column(Integer, primary_key=True)
    body = Column(Text, nullable=False)
    status = Column(String, nullable=False, default="open")   # 'open' | 'done'
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    resolved_at = Column(DateTime)


class VoiceDraft(Base):
    """A spoken log between "here's what I heard" and "Log it" (0071,
    docs/VOICE_LOGGING.md). `payload` is the structured interpretation; `result`
    lists the rows a commit wrote, so undo can remove exactly those."""
    __tablename__ = "voice_drafts"

    id = Column(String, primary_key=True)
    kind = Column(String, nullable=False)          # water | caffeine | alcohol | meal
    text = Column(Text, nullable=False)            # what was said
    payload = Column(Text, nullable=False)
    say = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    committed_at = Column(DateTime, index=True)
    result = Column(Text)
    undone_at = Column(DateTime)


class TzObservation(Base):
    """One Health Auto Export webhook's time-zone evidence (Alembic 0073).

    Time-zone awareness, phase 1: a record of where the phone's clock was, not yet read
    by anything. See services/tz.py.
    """
    __tablename__ = "tz_observations"

    id = Column(Integer, primary_key=True)
    observed_at = Column(DateTime, nullable=False)       # naive UTC, when the webhook arrived
    utc_offset_min = Column(Integer, nullable=False)     # offset on the newest sample
    latest_sample_utc = Column(DateTime)
    offsets_seen = Column(Text)                          # JSON {offset_min: count}
    n_samples = Column(Integer)
    source = Column(String, nullable=False, default="hae")


class TzPeriod(Base):
    """A manual "I was in this zone from start_date to end_date" (inclusive, local dates).

    Outranks observations. `tz_name` is an IANA zone ("Europe/Paris") or a fixed
    offset ("+03:00") when the zone itself isn't known.
    """
    __tablename__ = "tz_periods"

    id = Column(Integer, primary_key=True)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    tz_name = Column(String, nullable=False)
    source = Column(String, nullable=False, default="manual")
    note = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


class ParseJob(Base):
    """A food parse running in the background (Alembic 0075, 2026-10-21).

    "Parse in background" closes the log modal immediately; the server runs the SAME path
    as /api/nutrition/parse (repeat lookup → model → My Foods match) and keeps the result
    until it is reviewed and saved — so it survives iOS killing the backgrounded PWA,
    which an in-browser promise would not. Nothing is written to food_log until review.
    """
    __tablename__ = "parse_jobs"

    id = Column(String, primary_key=True)
    description = Column(Text, nullable=False)
    meal_type = Column(String, nullable=False)
    log_date = Column(Date, nullable=False)
    log_time = Column(String)                      # "HH:MM" or NULL (meal default)
    status = Column(String, nullable=False, default="pending")  # pending|done|error|saved|dismissed
    result = Column(Text)                          # JSON — the /parse response
    error = Column(Text)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    finished_at = Column(DateTime)


class CitationCheck(Base):
    """One run of the summary citation check (services/citations.py, Alembic 0076)."""
    __tablename__ = "citation_checks"

    id = Column(Integer, primary_key=True)
    kind = Column(String, nullable=False)          # daily | weekly | monthly
    period_key = Column(String, nullable=False)    # the date / week start / month
    checked_at = Column(DateTime, nullable=False)
    n_quoted = Column(Integer)                     # ρ + n values found in the final text
    bad_first = Column(Text)                       # JSON list — unsupported on first draft
    retried = Column(Boolean, default=False)
    bad_final = Column(Text)                       # JSON list — still unsupported when stored
    error = Column(Text)
