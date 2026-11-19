"""Baseline schema.

Revision ID: 0001
Revises:
Create Date: 2026-11-19

The private repository built this schema through 91 incremental migrations. For the
public snapshot they were collapsed into one: the statements below are the exact DDL
that running all of them on an empty SQLite database produced (sqlite_master), so a
fresh install gets the same tables, defaults and indexes the running app has.
New schema changes go in new revisions on top of this one, as before.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Tables first, then indexes, in the order SQLite created them.
STATEMENTS = [
    "CREATE TABLE health_metrics (\n\tid INTEGER NOT NULL, \n\tmetric_type VARCHAR NOT NULL, \n\ttimestamp DATETIME NOT NULL, \n\tvalue FLOAT NOT NULL, \n\tunit VARCHAR, \n\tsource VARCHAR, entry_channel VARCHAR, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_health_metrics_type_ts UNIQUE (metric_type, timestamp)\n)",
    "CREATE TABLE sleep_sessions (\n\tid INTEGER NOT NULL, \n\tdate DATE NOT NULL, \n\tbedtime DATETIME, \n\twake_time DATETIME, \n\ttotal_min INTEGER, \n\tdeep_min INTEGER, \n\tcore_min INTEGER, \n\trem_min INTEGER, \n\tawake_min INTEGER, \n\tsource VARCHAR, \n\tPRIMARY KEY (id), \n\tUNIQUE (date)\n)",
    "CREATE TABLE workouts (\n\tid INTEGER NOT NULL, \n\tapple_uuid VARCHAR NOT NULL, \n\tactivity_type VARCHAR, \n\tstart_time DATETIME NOT NULL, \n\tend_time DATETIME, \n\tduration_sec INTEGER, \n\tactive_calories FLOAT, \n\tavg_hr FLOAT, \n\tmax_hr FLOAT, \n\thr_samples JSON, \n\tsource VARCHAR, \n\trpe FLOAT, \n\tintensity_met FLOAT, trimp FLOAT, zone2_min FLOAT, zone3_min FLOAT, high_intensity_min FLOAT, zone1_min FLOAT, zone4_min FLOAT, zone5_min FLOAT, suspect BOOLEAN, suspect_reason VARCHAR, hr_peak FLOAT, hr_recovery_60 FLOAT, \n\tPRIMARY KEY (id), \n\tUNIQUE (apple_uuid)\n)",
    "CREATE TABLE daily_metrics (\n\tid INTEGER NOT NULL, \n\tdate DATE NOT NULL, \n\tdaily_load FLOAT, \n\tatl FLOAT, \n\tctl FLOAT, \n\tacwr FLOAT, \n\thrv_value FLOAT, \n\thrv_baseline FLOAT, \n\thrv_z_score FLOAT, \n\trhr_value FLOAT, \n\trhr_baseline FLOAT, \n\trhr_z_score FLOAT, \n\tsleep_debt_hours FLOAT, \n\twrist_temp_deviation FLOAT, \n\treadiness_score FLOAT, \n\treadiness_label VARCHAR, \n\tai_summary TEXT, \n\tcomputed_at DATETIME, zone2_pct_7d FLOAT, zone3_pct_7d FLOAT, high_intensity_pct_7d FLOAT, zone1_pct_7d FLOAT, hr_coverage_pct_7d FLOAT, zone4_pct_7d FLOAT, zone5_pct_7d FLOAT, ai_summary_inputs TEXT, \n\tPRIMARY KEY (id), \n\tUNIQUE (date)\n)",
    "CREATE TABLE body_weight_log (\n\tid INTEGER NOT NULL, \n\ttimestamp DATETIME NOT NULL, \n\tweight_kg FLOAT NOT NULL, \n\tnotes VARCHAR, \n\tPRIMARY KEY (id)\n)",
    "CREATE TABLE food_log (\n\tid INTEGER NOT NULL, \n\ttimestamp DATETIME NOT NULL, \n\tname VARCHAR, \n\tdescription TEXT NOT NULL, \n\tmeal_type VARCHAR DEFAULT 'snack', \n\tcalories FLOAT, \n\tprotein_g FLOAT, \n\tcarbs_g FLOAT, \n\tfat_g FLOAT, \n\tfiber_g FLOAT, \n\tsugar_g FLOAT, \n\tsodium_mg FLOAT, \n\tportion_quantity FLOAT, \n\tportion_unit VARCHAR, \n\tconfidence VARCHAR, \n\tllm_notes TEXT, \n\tis_saved BOOLEAN DEFAULT '0', skipped BOOLEAN DEFAULT 0, source_kind VARCHAR, source_id INTEGER, omitted_kcal INTEGER, time_estimated BOOLEAN, my_food_id INTEGER, servings FLOAT, combo_group VARCHAR, combo_part VARCHAR, combo_name VARCHAR, recipe_id INTEGER, brand VARCHAR, entry_channel VARCHAR, drink_kind VARCHAR, restaurant BOOLEAN, \n\tPRIMARY KEY (id)\n)",
    "CREATE TABLE user_settings (\n\tid INTEGER NOT NULL, \n\ttarget_sleep_hours FLOAT DEFAULT '8.0', \n\tmax_hr INTEGER DEFAULT '185', \n\tresting_hr_override INTEGER, \n\ttraining_goal VARCHAR DEFAULT 'building', \n\tsteps_goal INTEGER DEFAULT '10000', \n\tmove_goal INTEGER DEFAULT '500', \n\texercise_goal INTEGER DEFAULT '30', \n\tai_enabled BOOLEAN DEFAULT '1', \n\tprotein_goal_g INTEGER, \n\tcalorie_goal INTEGER, \n\theight_cm FLOAT, \n\tbirth_year INTEGER, \n\tbiological_sex VARCHAR, \n\tlast_webhook_at DATETIME, \n\tcaffeine_cutoff_hour INTEGER DEFAULT '14', \n\tlight_cutoff_hour INTEGER DEFAULT '10', calorie_goal_mode VARCHAR, weight_change_goal_lbs_per_week FLOAT, last_morning_pipeline_date DATE, caffeine_presets JSON, hrr_rest_ref INTEGER, fiber_goal_g INTEGER, alcohol_presets JSON, water_bottle_oz INTEGER, water_presets JSON, \n\tPRIMARY KEY (id)\n)",
    "CREATE TABLE weekly_summaries (\n\tid INTEGER NOT NULL, \n\tweek_start DATE NOT NULL, \n\tweek_end DATE NOT NULL, \n\tsummary_text TEXT, \n\tmodel VARCHAR, \n\tcreated_at DATETIME, \n\tPRIMARY KEY (id), \n\tUNIQUE (week_start)\n)",
    "CREATE TABLE monthly_summaries (\n\tid INTEGER NOT NULL, \n\tyear_month VARCHAR NOT NULL, \n\tmonth_start DATE NOT NULL, \n\tmonth_end DATE NOT NULL, \n\tsummary_text TEXT, \n\tmodel VARCHAR, \n\tcreated_at DATETIME, \n\tPRIMARY KEY (id), \n\tUNIQUE (year_month)\n)",
    "CREATE TABLE insight_cache (\n\tid INTEGER NOT NULL, \n\tmetric_a VARCHAR NOT NULL, \n\tmetric_b VARCHAR NOT NULL, \n\tlag_days INTEGER DEFAULT '0', \n\tpearson_r FLOAT, \n\tn INTEGER, \n\tdays_window INTEGER DEFAULT '90', \n\tcomputed_at DATETIME, n_eff INTEGER, p_value FLOAT, ci_low FLOAT, ci_high FLOAT, spearman_r FLOAT, prev_spearman_r FLOAT, prev_n INTEGER, reverse_lag_days INTEGER, reverse_spearman_r FLOAT, reverse_n INTEGER, reverse_ci_low FLOAT, reverse_ci_high FLOAT, fwd_rev_diff FLOAT, fwd_rev_diff_low FLOAT, fwd_rev_diff_high FLOAT, forward_exceeds_reverse BOOLEAN, untestable_reason VARCHAR, survivor_details JSON, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_insight_cache UNIQUE (metric_a, metric_b, lag_days)\n)",
    "CREATE TABLE api_call_log (\n\tid INTEGER NOT NULL, \n\tfeature VARCHAR, \n\tmodel VARCHAR, \n\tinput_tokens INTEGER, \n\toutput_tokens INTEGER, \n\tcost_usd FLOAT, \n\tcreated_at DATETIME, \n\tPRIMARY KEY (id)\n)",
    "CREATE TABLE habit_goals (\n\thabit_key VARCHAR NOT NULL, \n\ttarget INTEGER NOT NULL, \n\tperiod VARCHAR NOT NULL, \n\tPRIMARY KEY (habit_key)\n)",
    "CREATE TABLE \"habit_logs\" (\n\tdate DATE NOT NULL, \n\tno_late_caffeine BOOLEAN, \n\toutdoor_light_am BOOLEAN, \n\tvitamin_d BOOLEAN, \n\tmagnesium BOOLEAN, \n\tsupplements BOOLEAN, \n\tmeditation BOOLEAN, \n\tnotes VARCHAR, \n\tcreatine BOOLEAN, ate_late BOOLEAN, screens_in_bed BOOLEAN, caffeine_last_min INTEGER, meditation_min INTEGER, \n\tPRIMARY KEY (date)\n)",
    "CREATE TABLE interventions (\n\tid INTEGER NOT NULL, \n\tdate DATE NOT NULL, \n\tlabel VARCHAR NOT NULL, \n\tnotes VARCHAR, \n\tcreated_at DATETIME, category VARCHAR, \n\tPRIMARY KEY (id)\n)",
    "CREATE TABLE hevy_workouts (\n\tid VARCHAR NOT NULL, \n\ttitle VARCHAR, \n\tstart_time DATETIME NOT NULL, \n\tend_time DATETIME, \n\thevy_updated_at DATETIME, \n\tapple_uuid VARCHAR, \n\ttonnage_kg FLOAT, \n\tworking_sets INTEGER, \n\tavg_top_set_rpe FLOAT, \n\tsynced_at DATETIME, pr_notified_at DATETIME, \n\tPRIMARY KEY (id)\n)",
    "CREATE TABLE hevy_sets (\n\tid INTEGER NOT NULL, \n\thevy_workout_id VARCHAR NOT NULL, \n\texercise_index INTEGER, \n\texercise_title VARCHAR, \n\texercise_template_id VARCHAR, \n\tsuperset_id INTEGER, \n\tset_index INTEGER, \n\tset_type VARCHAR, \n\tweight_kg FLOAT, \n\treps INTEGER, \n\tdistance_meters FLOAT, \n\tduration_seconds INTEGER, \n\trpe FLOAT, \n\tPRIMARY KEY (id)\n)",
    "CREATE TABLE hevy_exercise_templates (\n\tid VARCHAR NOT NULL, \n\ttitle VARCHAR, \n\texercise_type VARCHAR, \n\tprimary_muscle_group VARCHAR, \n\tsecondary_muscle_groups JSON, \n\tis_custom BOOLEAN, \n\tfetched_at DATETIME, \n\tPRIMARY KEY (id)\n)",
    "CREATE TABLE hevy_sync_state (\n\tid INTEGER NOT NULL, \n\tlast_event_check DATETIME, \n\tbackfill_done BOOLEAN, \n\tlast_sync_at DATETIME, \n\tlast_error VARCHAR, \n\tPRIMARY KEY (id)\n)",
    "CREATE TABLE finding_ledger (\n\tid INTEGER NOT NULL, \n\tmetric_a VARCHAR NOT NULL, \n\tmetric_b VARCHAR NOT NULL, \n\tlag_days INTEGER, \n\tfirst_suggestive_at DATE, \n\trho_at_suggestive FLOAT, \n\tn_at_suggestive INTEGER, \n\tfirst_confirmed_at DATE, \n\trho_at_confirmed FLOAT, \n\tn_at_confirmed INTEGER, days_window INTEGER, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_finding_ledger UNIQUE (metric_a, metric_b, lag_days)\n)",
    "CREATE TABLE \"subjective_log\" (\n\tid INTEGER NOT NULL, \n\tdate DATE NOT NULL, \n\twellbeing_score FLOAT, \n\tnotes TEXT, \n\tsleep_quality FLOAT, \n\tenergy FLOAT, \n\tmood FLOAT, \n\tsoreness TEXT, \n\tstress FLOAT, \n\tsick BOOLEAN, \n\tday_score FLOAT, \n\tPRIMARY KEY (id), \n\tUNIQUE (date)\n)",
    "CREATE TABLE experiments (\n\tid INTEGER NOT NULL, \n\tlabel VARCHAR NOT NULL, \n\tarm_a VARCHAR NOT NULL, \n\tarm_b VARCHAR NOT NULL, \n\tprimary_outcome VARCHAR, \n\tstarted_at DATE, \n\tended_at DATE, \n\tnotes TEXT, kind VARCHAR DEFAULT 'training' NOT NULL, target_per_arm INTEGER DEFAULT '30' NOT NULL, \n\tPRIMARY KEY (id)\n)",
    "CREATE TABLE experiment_assignments (\n\tid INTEGER NOT NULL, \n\texperiment_id INTEGER NOT NULL, \n\tdate DATE NOT NULL, \n\tarm VARCHAR NOT NULL, \n\treadiness_at_assignment FLOAT, \n\thrv_z_at_assignment FLOAT, \n\tcreated_at DATETIME, eligible_confirmed BOOLEAN, ineligible_reason VARCHAR, complied BOOLEAN, dose_minute INTEGER, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_experiment_assignment_day UNIQUE (experiment_id, date)\n)",
    "CREATE TABLE blood_markers (\n\tid INTEGER NOT NULL, \n\tdate DATE NOT NULL, \n\tmarker VARCHAR NOT NULL, \n\tlabel VARCHAR, \n\tvalue FLOAT NOT NULL, \n\tunit VARCHAR, \n\tref_low FLOAT, \n\tref_high FLOAT, \n\tcreated_at DATETIME, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_blood_marker_day UNIQUE (date, marker)\n)",
    "CREATE TABLE screentime_events (\n\tid INTEGER NOT NULL, \n\tapp VARCHAR NOT NULL, \n\tduration_sec INTEGER NOT NULL, \n\tstart_time DATETIME NOT NULL, \n\tend_time DATETIME, \n\tdevice VARCHAR NOT NULL, \n\tcategory VARCHAR, \n\tcreated_at DATETIME, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_screentime_app_start_device UNIQUE (app, start_time, device)\n)",
    "CREATE TABLE weather_daily (\n\tdate DATE NOT NULL, \n\ttemp_mean FLOAT, \n\ttemp_min FLOAT, \n\ttemp_max FLOAT, \n\tapparent_max FLOAT, \n\thumidity_mean FLOAT, \n\tpressure_mean FLOAT, \n\tpressure_min FLOAT, \n\tpressure_delta FLOAT, \n\tuv_max FLOAT, \n\tprecip_sum FLOAT, \n\taqi_mean FLOAT, \n\taqi_max FLOAT, \n\tpm25_mean FLOAT, \n\tcondition VARCHAR, \n\tupdated_at DATETIME, sunrise DATETIME, sunset DATETIME, aqi_source VARCHAR, aqi_pollutant VARCHAR, aqi_samples INTEGER DEFAULT '0', aqi_last_obs VARCHAR, \n\tPRIMARY KEY (date)\n)",
    "CREATE TABLE day_flags (\n\tid INTEGER NOT NULL, \n\tdate DATE NOT NULL, \n\treason VARCHAR NOT NULL, \n\tsource VARCHAR DEFAULT 'manual', \n\tnote TEXT, \n\tcreated_at DATETIME, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_day_flag UNIQUE (date, reason)\n)",
    "CREATE TABLE caffeine_log (\n\tid INTEGER NOT NULL, \n\ttimestamp DATETIME NOT NULL, \n\tkind VARCHAR NOT NULL, \n\tmg FLOAT NOT NULL, \n\tquantity FLOAT DEFAULT '1.0', entry_channel VARCHAR, \n\tPRIMARY KEY (id)\n)",
    "CREATE TABLE caffeine_day (\n\tdate DATE NOT NULL, \n\tack VARCHAR NOT NULL, \n\tPRIMARY KEY (date)\n)",
    "CREATE TABLE game_stat_daily (\n\tdate DATE NOT NULL, \n\tstat VARCHAR NOT NULL, \n\traw FLOAT, \n\tscore FLOAT, \n\txp FLOAT, \n\tcum_xp FLOAT, \n\tlevel INTEGER, components JSON, \n\tPRIMARY KEY (date, stat)\n)",
    "CREATE TABLE pvt_session (\n\tid INTEGER NOT NULL, \n\tdate DATE NOT NULL, \n\tstarted_at DATETIME NOT NULL, \n\tlocal_minute INTEGER NOT NULL, \n\tduration_s FLOAT, \n\tn_trials INTEGER, \n\tmedian_rt FLOAT, \n\tmean_rt FLOAT, \n\tmean_reciprocal_rt FLOAT, \n\tslowest10_mean FLOAT, \n\tfastest10_mean FLOAT, \n\tlapses_355 INTEGER, \n\tlapses_500 INTEGER, \n\tfalse_starts INTEGER, \n\ttrials JSON, \n\tdevice VARCHAR, \n\tcreated_at DATETIME, invalid_trials INTEGER, protocol INTEGER DEFAULT '1' NOT NULL, timeouts INTEGER, timing_suspect BOOLEAN DEFAULT 0, timing_problems JSON, perf_score FLOAT, tot_slope FLOAT, slowest10_speed FLOAT, interrupted BOOLEAN DEFAULT 0, \n\tPRIMARY KEY (id)\n)",
    "CREATE TABLE blood_marker_targets (\n\tmarker VARCHAR NOT NULL, \n\ttarget_low FLOAT, \n\ttarget_high FLOAT, \n\tupdated_at DATETIME, \n\tPRIMARY KEY (marker)\n)",
    "CREATE TABLE energy_calibration (\n\tid INTEGER NOT NULL, \n\tfitted_at DATETIME NOT NULL, \n\tk FLOAT NOT NULL, \n\tk_raw FLOAT, \n\tk_ci_low FLOAT, \n\tk_ci_high FLOAT, \n\trmr FLOAT, \n\trmr_basis VARCHAR, \n\tmean_active FLOAT, \n\tadaptive_tdee FLOAT, \n\tn_intake_days INTEGER, \n\twindow_days INTEGER, \n\tPRIMARY KEY (id)\n)",
    "CREATE TABLE kss_log (\n\tid INTEGER NOT NULL, \n\tdate DATE NOT NULL, \n\tstarted_at DATETIME NOT NULL, \n\tlocal_minute INTEGER NOT NULL, \n\tscore INTEGER NOT NULL, \n\tdevice VARCHAR, \n\tcreated_at DATETIME DEFAULT CURRENT_TIMESTAMP, \n\tPRIMARY KEY (id)\n)",
    "CREATE TABLE tapping_session (\n\tid INTEGER NOT NULL, \n\tdate DATE NOT NULL, \n\tstarted_at DATETIME NOT NULL, \n\tlocal_minute INTEGER NOT NULL, \n\tprotocol INTEGER DEFAULT '1' NOT NULL, \n\tduration_ms INTEGER, \n\thand VARCHAR, \n\tn_taps INTEGER, \n\ttap_rate FLOAT, \n\tmean_iti FLOAT, \n\titi_sd FLOAT, \n\titi_cv FLOAT, \n\tfirst_half_rate FLOAT, \n\tsecond_half_rate FLOAT, \n\trate_decrement_pct FLOAT, \n\tartifacts INTEGER, \n\ttaps JSON, \n\tdevice VARCHAR, \n\tinterrupted BOOLEAN DEFAULT 0, \n\tcreated_at DATETIME DEFAULT CURRENT_TIMESTAMP, \n\tPRIMARY KEY (id)\n)",
    "CREATE TABLE supplements (\n\tid INTEGER NOT NULL, \n\tname VARCHAR NOT NULL, \n\tdose FLOAT, \n\tunit VARCHAR, \n\ttiming VARCHAR, \n\tstarted_on DATE NOT NULL, \n\tended_on DATE, \n\tnotes TEXT, \n\tcreated_at DATETIME DEFAULT CURRENT_TIMESTAMP, \n\tPRIMARY KEY (id)\n)",
    "CREATE TABLE app_notes (\n\tid INTEGER NOT NULL, \n\tbody TEXT NOT NULL, \n\tstatus VARCHAR DEFAULT 'open' NOT NULL, \n\tcreated_at DATETIME NOT NULL, \n\tresolved_at DATETIME, \n\tPRIMARY KEY (id)\n)",
    "CREATE TABLE food_item_tags (\n\tid INTEGER NOT NULL, \n\titem_key VARCHAR NOT NULL, \n\tdisplay_name VARCHAR, \n\tprimary_cat VARCHAR, \n\tgroups TEXT, \n\tsource VARCHAR DEFAULT 'llm' NOT NULL, \n\tmodel VARCHAR, \n\tfailed BOOLEAN DEFAULT '0' NOT NULL, \n\tcreated_at DATETIME, \n\tupdated_at DATETIME, components TEXT, \n\tPRIMARY KEY (id)\n)",
    "CREATE TABLE metric_ack (\n\tmetric VARCHAR NOT NULL, \n\tacked_at DATETIME, \n\tacked_until DATE NOT NULL, \n\tnote VARCHAR, \n\tPRIMARY KEY (metric)\n)",
    "CREATE TABLE body_measurements (\n\tid INTEGER NOT NULL, \n\tdate DATE NOT NULL, \n\twaist_cm FLOAT, \n\tarm_cm FLOAT, \n\tchest_cm FLOAT, \n\tnotes VARCHAR, \n\tcreated_at DATETIME, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_body_measurements_date UNIQUE (date)\n)",
    "CREATE TABLE food_parse_cache (\n\tid INTEGER NOT NULL, \n\ttext_key VARCHAR NOT NULL, \n\titems_json TEXT NOT NULL, \n\thits INTEGER DEFAULT '0' NOT NULL, \n\tupdated_at DATETIME, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_food_parse_cache_text_key UNIQUE (text_key)\n)",
    "CREATE TABLE ui_views (\n\tid INTEGER NOT NULL, \n\t\"view\" VARCHAR NOT NULL, \n\tts DATETIME NOT NULL, \n\tPRIMARY KEY (id)\n)",
    "CREATE TABLE my_foods (\n\tid INTEGER NOT NULL, \n\tname VARCHAR NOT NULL, \n\titem_key VARCHAR NOT NULL, \n\trole VARCHAR DEFAULT 'food' NOT NULL, \n\tmeal_type VARCHAR, \n\tserving_label VARCHAR, \n\tserving_qty FLOAT DEFAULT '1' NOT NULL, \n\tserving_unit VARCHAR DEFAULT 'serving' NOT NULL, \n\tunits TEXT, \n\tcalories FLOAT NOT NULL, \n\tprotein_g FLOAT, \n\tcarbs_g FLOAT, \n\tfat_g FLOAT, \n\tfiber_g FLOAT, \n\tsugar_g FLOAT, \n\tsodium_mg FLOAT, \n\tsource VARCHAR DEFAULT 'confirmed' NOT NULL, \n\tnotes TEXT, \n\tarchived BOOLEAN DEFAULT '0' NOT NULL, \n\tcreated_at DATETIME, \n\tupdated_at DATETIME, aliases TEXT, components TEXT, drink_kind VARCHAR, abv FLOAT, brand VARCHAR, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_my_foods_item_key UNIQUE (item_key)\n)",
    "CREATE TABLE my_food_backfills (\n\tid INTEGER NOT NULL, \n\tmy_food_id INTEGER NOT NULL, \n\tcreated_at DATETIME, \n\tn_rows INTEGER DEFAULT '0' NOT NULL, \n\trows_json TEXT NOT NULL, \n\tundone_at DATETIME, \n\tPRIMARY KEY (id)\n)",
    "CREATE TABLE my_food_dismissals (\n\tid INTEGER NOT NULL, \n\titem_key VARCHAR NOT NULL, \n\tcreated_at DATETIME, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_my_food_dismissals_item_key UNIQUE (item_key)\n)",
    "CREATE TABLE recipe_nutrition (\n\tid INTEGER NOT NULL, \n\trecipe_id INTEGER NOT NULL, \n\ttitle VARCHAR NOT NULL, \n\tservings FLOAT, \n\tcalories FLOAT, \n\tprotein_g FLOAT, \n\tcarbs_g FLOAT, \n\tfat_g FLOAT, \n\tfiber_g FLOAT, \n\tsugar_g FLOAT, \n\tsodium_mg FLOAT, \n\tmeal_type VARCHAR, \n\tsource VARCHAR DEFAULT 'estimate' NOT NULL, \n\tsource_hash VARCHAR, \n\tnotes TEXT, \n\thidden BOOLEAN DEFAULT 0 NOT NULL, \n\tcreated_at DATETIME, \n\tupdated_at DATETIME, total_json TEXT, total_weight_g FLOAT, servings_basis VARCHAR, serving_description VARCHAR, assumptions TEXT, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_recipe_nutrition_recipe_id UNIQUE (recipe_id)\n)",
    "CREATE TABLE voice_drafts (\n\tid VARCHAR NOT NULL, \n\tkind VARCHAR NOT NULL, \n\ttext TEXT NOT NULL, \n\tpayload TEXT NOT NULL, \n\tsay TEXT NOT NULL, \n\tcreated_at DATETIME NOT NULL, \n\texpires_at DATETIME NOT NULL, \n\tcommitted_at DATETIME, \n\tresult TEXT, \n\tundone_at DATETIME, \n\tPRIMARY KEY (id)\n)",
    "CREATE TABLE tz_observations (\n\tid INTEGER NOT NULL, \n\tobserved_at DATETIME NOT NULL, \n\tutc_offset_min INTEGER NOT NULL, \n\tlatest_sample_utc DATETIME, \n\toffsets_seen TEXT, \n\tn_samples INTEGER, \n\tsource VARCHAR DEFAULT 'hae' NOT NULL, \n\tPRIMARY KEY (id)\n)",
    "CREATE TABLE tz_periods (\n\tid INTEGER NOT NULL, \n\tstart_date DATE NOT NULL, \n\tend_date DATE NOT NULL, \n\ttz_name VARCHAR NOT NULL, \n\tsource VARCHAR DEFAULT 'manual' NOT NULL, \n\tnote VARCHAR, \n\tcreated_at DATETIME DEFAULT CURRENT_TIMESTAMP, \n\tPRIMARY KEY (id)\n)",
    "CREATE TABLE parse_jobs (\n\tid VARCHAR NOT NULL, \n\tdescription TEXT NOT NULL, \n\tmeal_type VARCHAR NOT NULL, \n\tlog_date DATE NOT NULL, \n\tlog_time VARCHAR, \n\tstatus VARCHAR DEFAULT 'pending' NOT NULL, \n\tresult TEXT, \n\terror TEXT, \n\tcreated_at DATETIME NOT NULL, \n\tfinished_at DATETIME, \n\tPRIMARY KEY (id)\n)",
    "CREATE TABLE citation_checks (\n\tid INTEGER NOT NULL, \n\tkind VARCHAR NOT NULL, \n\tperiod_key VARCHAR NOT NULL, \n\tchecked_at DATETIME NOT NULL, \n\tn_quoted INTEGER, \n\tbad_first TEXT, \n\tretried BOOLEAN DEFAULT 0, \n\tbad_final TEXT, \n\terror TEXT, \n\tPRIMARY KEY (id)\n)",
    "CREATE TABLE idempotency_keys (\n\t\"key\" VARCHAR NOT NULL, \n\tpath VARCHAR, \n\tstatus INTEGER NOT NULL, \n\tbody TEXT, \n\tcontent_type VARCHAR, \n\tcreated_at DATETIME NOT NULL, \n\tPRIMARY KEY (\"key\")\n)",
    "CREATE TABLE error_events (\n\t\"key\" VARCHAR NOT NULL, \n\tday VARCHAR NOT NULL, \n\tlogger VARCHAR, \n\ttemplate TEXT, \n\tsample TEXT, \n\tcount INTEGER DEFAULT '0' NOT NULL, \n\tfirst_at DATETIME, \n\tlast_at DATETIME, \n\tPRIMARY KEY (\"key\", day)\n)",
    "CREATE TABLE body_scans (\n\tid INTEGER NOT NULL, \n\tdate DATE NOT NULL, \n\ttime VARCHAR, \n\tdevice VARCHAR, \n\tweight_lb FLOAT NOT NULL, \n\tbody_fat_pct FLOAT NOT NULL, \n\tfat_mass_lb FLOAT, \n\tlean_mass_lb FLOAT, \n\tsmm_lb FLOAT, \n\ttbw_lb FLOAT, \n\tsegments JSON, \n\tnotes VARCHAR, \n\tcreated_at DATETIME, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_body_scans_date UNIQUE (date)\n)",
    "CREATE TABLE \"class_boards\" (\n\tid INTEGER NOT NULL, \n\tdate DATE NOT NULL, \n\tcreated_at DATETIME, \n\tsource VARCHAR, \n\tmodel VARCHAR, \n\tms INTEGER, \n\titems JSON, \n\tunreadable JSON, \n\tpicks JSON, \n\teasy BOOLEAN DEFAULT 0, outcome JSON, checked_at DATETIME, \n\tPRIMARY KEY (id)\n)",
    "CREATE TABLE class_aliases (\n\tid INTEGER NOT NULL, \n\t\"key\" VARCHAR NOT NULL, \n\ttitle VARCHAR NOT NULL, \n\tsource VARCHAR, \n\tseen INTEGER DEFAULT '1', \n\tupdated_at DATETIME, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_class_aliases_key UNIQUE (\"key\")\n)",
    "CREATE TABLE health_events (\n\tid INTEGER NOT NULL, \n\tdate DATE NOT NULL, \n\tkind VARCHAR NOT NULL, \n\tname VARCHAR NOT NULL, \n\tnotes VARCHAR, \n\tcreated_at DATETIME, \n\tPRIMARY KEY (id)\n)",
    "CREATE TABLE tv_sessions (\n\tid INTEGER NOT NULL, \n\tstart_time DATETIME NOT NULL, \n\tend_time DATETIME NOT NULL, \n\tdate DATE NOT NULL, \n\tminutes FLOAT NOT NULL, \n\tsegments JSON NOT NULL, \n\tmine BOOLEAN, \n\tanswered_at DATETIME, watcher VARCHAR, \n\tPRIMARY KEY (id), \n\tUNIQUE (start_time)\n)",
    "CREATE TABLE switch_steps (\n\tid INTEGER NOT NULL, \n\ttime DATETIME NOT NULL, \n\tminutes FLOAT NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (time)\n)",
    "CREATE TABLE trip_gaps (\n\tid INTEGER NOT NULL, \n\tstart_date DATE NOT NULL, \n\tend_date DATE NOT NULL, \n\titems JSON NOT NULL, \n\tupdated_at DATETIME, \n\tPRIMARY KEY (id), \n\tUNIQUE (start_date)\n)",
    "CREATE INDEX ix_hevy_workouts_start ON hevy_workouts (start_time)",
    "CREATE INDEX ix_hevy_sets_workout ON hevy_sets (hevy_workout_id)",
    "CREATE INDEX ix_hevy_sets_template ON hevy_sets (exercise_template_id)",
    "CREATE INDEX ix_screentime_start ON screentime_events (start_time)",
    "CREATE INDEX ix_day_flags_date ON day_flags (date)",
    "CREATE INDEX ix_caffeine_log_ts ON caffeine_log (timestamp)",
    "CREATE INDEX ix_game_stat_daily_stat_date ON game_stat_daily (stat, date)",
    "CREATE INDEX ix_pvt_session_date ON pvt_session (date)",
    "CREATE INDEX ix_energy_calibration_fitted ON energy_calibration (fitted_at)",
    "CREATE INDEX ix_kss_log_date ON kss_log (date)",
    "CREATE INDEX ix_tapping_session_date ON tapping_session (date)",
    "CREATE INDEX ix_supplements_started_on ON supplements (started_on)",
    "CREATE INDEX ix_food_log_source ON food_log (source_kind, source_id)",
    "CREATE INDEX ix_app_notes_status ON app_notes (status, created_at)",
    "CREATE UNIQUE INDEX ix_food_item_tags_key ON food_item_tags (item_key)",
    "CREATE INDEX ix_ui_views_view ON ui_views (\"view\")",
    "CREATE INDEX ix_my_food_backfills_my_food_id ON my_food_backfills (my_food_id)",
    "CREATE INDEX ix_food_log_my_food_id ON food_log (my_food_id)",
    "CREATE INDEX ix_food_log_combo_group ON food_log (combo_group)",
    "CREATE INDEX ix_food_log_recipe_id ON food_log (recipe_id)",
    "CREATE INDEX ix_voice_drafts_committed ON voice_drafts (committed_at)",
    "CREATE INDEX ix_tz_observations_observed_at ON tz_observations (observed_at)",
    "CREATE INDEX ix_parse_jobs_status ON parse_jobs (status)",
    "CREATE INDEX ix_class_boards_date ON class_boards (date)",
    "CREATE INDEX ix_health_events_date ON health_events (date)",
    "CREATE INDEX ix_tv_sessions_date ON tv_sessions (date)",
]

TABLES = ["health_metrics", "sleep_sessions", "workouts", "daily_metrics", "body_weight_log", "food_log", "user_settings", "weekly_summaries", "monthly_summaries", "insight_cache", "api_call_log", "habit_goals", "habit_logs", "interventions", "hevy_workouts", "hevy_sets", "hevy_exercise_templates", "hevy_sync_state", "finding_ledger", "subjective_log", "experiments", "experiment_assignments", "blood_markers", "screentime_events", "weather_daily", "day_flags", "caffeine_log", "caffeine_day", "game_stat_daily", "pvt_session", "blood_marker_targets", "energy_calibration", "kss_log", "tapping_session", "supplements", "app_notes", "food_item_tags", "metric_ack", "body_measurements", "food_parse_cache", "ui_views", "my_foods", "my_food_backfills", "my_food_dismissals", "recipe_nutrition", "voice_drafts", "tz_observations", "tz_periods", "parse_jobs", "citation_checks", "idempotency_keys", "error_events", "body_scans", "class_boards", "class_aliases", "health_events", "tv_sessions", "switch_steps", "trip_gaps"]


def upgrade() -> None:
    for sql in STATEMENTS:
        op.execute(sql)


def downgrade() -> None:
    for name in reversed(TABLES):
        op.execute(f'DROP TABLE IF EXISTS "{name}"')
