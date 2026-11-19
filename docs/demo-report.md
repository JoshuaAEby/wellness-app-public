# Demo data: planted effects vs what the engine found

Pairs tested: 425. Surviving (significant or suggestive): 34.

- Planted effects recovered: **7 of 8**
- Trap pairs surviving: 6 (labelled CONFOUNDED: 6)
- Co-occurrence survivors (drink calories riding along with alcohol): 4
- Wrong-sign survivors: 0
- False positives (no path in the generator): 4
- Negative controls surviving: 0

## Recovered

| pair | lag | ρ | n | tier | reverse-lag check |
|---|---|---|---|---|---|
| `sleep_total_h` → `subj_sleep` | 0 | +0.50 | 319 | significant | passes | — More sleep, better rated sleep
| `alcohol_sdu` → `rhr_z_score` | 1 | +0.50 | 345 | significant | passes | — Drinks raise overnight heart rate the next night
| `alcohol_onboard_bed` → `rhr_z_score` | 1 | +0.41 | 346 | significant | passes | — Drinks raise overnight heart rate the next night
| `bedtime_hour` → `subj_sleep` | 0 | -0.40 | 319 | significant | passes | — More sleep, better rated sleep
| `sleep_total_h` → `subj_energy` | 0 | +0.36 | 319 | significant | passes | — More sleep, more energy the same morning (later bedtime = less sleep)
| `bedtime_hour` → `subj_energy` | 0 | -0.30 | 319 | significant | passes | — More sleep, more energy the same morning (later bedtime = less sleep)
| `caffeine_last_hour` → `subj_sleep` | 1 | -0.30 | 306 | significant | passes | — Caffeine still on board at bedtime worsens rated sleep
| `alcohol_sdu` → `hrv_z_score` | 1 | -0.30 | 344 | significant | passes | — Drinks lower overnight HRV the next night
| `alcohol_sdu` → `respiratory_rate` | 1 | +0.29 | 345 | significant | passes | — Drinks raise sleeping breathing rate the next night
| `alcohol_onboard_bed` → `hrv_z_score` | 1 | -0.27 | 345 | significant | passes | — Drinks lower overnight HRV the next night
| `caffeine_total_mg` → `subj_sleep` | 1 | -0.27 | 306 | significant | passes | — Caffeine still on board at bedtime worsens rated sleep
| `alcohol_onboard_bed` → `respiratory_rate` | 1 | +0.26 | 346 | significant | passes | — Drinks raise sleeping breathing rate the next night
| `habit_caffeine` → `subj_sleep` | 1 | +0.26 | 306 | significant | passes | — Caffeine still on board at bedtime worsens rated sleep
| `caffeine_onboard_bed` → `subj_sleep` | 1 | -0.26 | 306 | significant | passes | — Caffeine still on board at bedtime worsens rated sleep
| `hard_minutes` → `hrv_z_score` | 1 | -0.23 | 345 | significant | passes | — A hard class lowers HRV the next morning
| `exercise_minutes` → `hrv_z_score` | 1 | -0.20 | 249 | significant | passes | — A hard class lowers HRV the next morning
| `daily_load` → `hrv_z_score` | 1 | -0.18 | 345 | significant | inconclusive | — A hard class lowers HRV the next morning
| `active_calories` → `hrv_z_score` | 1 | -0.17 | 345 | significant | inconclusive | — A hard class lowers HRV the next morning
| `acwr` → `hrv_z_score` | 1 | -0.15 | 330 | suggestive | not_testable | — A hard class lowers HRV the next morning
| `workout_hiit` → `hrv_z_score` | 1 | -0.14 | 345 | suggestive | passes | — A hard class lowers HRV the next morning

## Trap

| pair | lag | ρ | n | tier | reverse-lag check |
|---|---|---|---|---|---|
| `caffeine_onboard_bed` → `subj_stress` | 0 | +0.31 | 309 | significant | confounded | — Hidden stress weeks drive both caffeine and rated stress
| `caffeine_total_mg` → `subj_stress` | 1 | +0.28 | 306 | significant | confounded | — Hidden stress weeks drive both caffeine and rated stress
| `caffeine_onboard_bed` → `subj_stress` | 1 | +0.26 | 306 | significant | confounded | — Hidden stress weeks drive both caffeine and rated stress
| `caffeine_total_mg` → `subj_mood` | 1 | -0.26 | 306 | significant | confounded | — Hidden stress weeks drive both caffeine and low mood
| `caffeine_onboard_bed` → `subj_mood` | 1 | -0.25 | 306 | significant | confounded | — Hidden stress weeks drive both caffeine and low mood
| `caffeine_last_hour` → `subj_stress` | 1 | +0.15 | 306 | suggestive | confounded | — Hidden stress weeks drive both caffeine and rated stress

## Co-occurrence

| pair | lag | ρ | n | tier | reverse-lag check |
|---|---|---|---|---|---|
| `calories` → `rhr_z_score` | 1 | +0.30 | 328 | significant | passes | — Drink calories ride along with alcohol (same-day co-occurrence)
| `calories` → `respiratory_rate` | 1 | +0.21 | 328 | significant | passes | — Drink calories ride along with alcohol (same-day co-occurrence)
| `calories` → `hrv_z_score` | 1 | -0.20 | 327 | significant | passes | — Drink calories ride along with alcohol (same-day co-occurrence)
| `energy_balance` → `rhr_z_score` | 1 | +0.20 | 328 | significant | passes | — Drink calories ride along with alcohol (same-day co-occurrence)

## False

| pair | lag | ρ | n | tier | reverse-lag check |
|---|---|---|---|---|---|
| `zone2_pct_7d` → `subj_mood` | 1 | +0.21 | 315 | suggestive | not_testable |
| `time_in_daylight` → `subj_day` | 0 | -0.21 | 173 | suggestive | inconclusive |
| `fiber_g` → `subj_sleep` | 1 | -0.17 | 300 | suggestive | inconclusive |
| `habit_light` → `subj_stress` | 1 | +0.15 | 316 | suggestive | passes |

## Traps that produced no surviving pair

- Energy drives steps (reverse causation), and energy persists day to day (`steps` → `subj_energy`)

## Planted but not found

- **A hard class raises overnight heart rate slightly (deliberately small)** (+0.3 bpm after a typical class). Strongest tested pair: `workout_hiit` lag 1, ρ +0.10, n 346.

