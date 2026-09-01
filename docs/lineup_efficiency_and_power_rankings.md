# Design: Lineup Efficiency & Manager Power Rankings

Drafted 2026-08-30. Two new analysis features following the existing `features/*.py` -> CSV
pattern.

**Status: implemented (v1, CSV only).** `features/lineup_efficiency.py`,
`features/manager_power_rankings.py`, tests, and `main.py` wiring are in. The two open
questions below are deliberately deferred for v1.

## Shared prerequisite

Both features consume weekly per-player lineup data. `features/build_players_weekly.py` is
already built but never called from `main.py`. It must be wired in first (CSV output at
`reports/players_weekly.csv`, optional `players_weekly` DB table), the same treatment
`build_matchups` got.

Relevant columns it already produces: `week`, `fantasy_team_id`, `fantasy_team`,
`player_id`, `player_name`, `points`, `lineup_slot`, `eligible_slots`, `was_started`
(`lineup_slot not in ["BE", "IR"]`), `injury_status`, `injured`.

New dependency: **scipy** (for `scipy.optimize.linear_sum_assignment`). Add to
`requirements.txt`.

---

## 1. Lineup Efficiency

**File:** `features/lineup_efficiency.py`
**Output:** `reports/lineup_efficiency.csv`

### Per team per week

```
actual_points  = sum(points)  where was_started
optimal_points = max total points obtainable from that week's rostered players,
                 assigned to that week's starting slots, respecting eligible_slots
efficiency     = actual_points / optimal_points        # 1.0 = perfect management
points_left    = optimal_points - actual_points
```

### Computing `optimal_points` (max-weight bipartite assignment)

- **Starting slots for the week:** the multiset of `lineup_slot` values used that week that
  are not `BE` / `IR` (e.g. `PG, SG, SF, PF, C, G, F, UT, UT, UT`). Derived per week so it
  adapts to league config and to weeks where a slot went unused.
- **Candidate players:** every player on the team's roster that week (started or benched).
  - **Exclude players whose `injury_status` was `OUT`** (also treat `injured is True` +
    status in `{OUT, SUSPENSION}` as unplayable). Rationale: the metric measures decisions
    the manager could actually make; you can't be dinged for a player the platform wouldn't
    let you start. Players listed `DAY_TO_DAY` / `QUESTIONABLE` stay in the pool.
  - `IR`-slot players are excluded from the starting pool (they can't be started).
- **Assignment:** build a `players x slots` value matrix. Entry = player's `points` if the
  slot name is in that player's `eligible_slots` (with `UT`/`G`/`F` flex handled by ESPN's
  own `eligible_slots` list), else `-inf` / disallowed. Run
  `scipy.optimize.linear_sum_assignment` on the negated matrix to maximize total points.
  Pad with zero-value dummy players if fewer eligible players than slots.

### Season report — one row per team

| field | meaning |
| --- | --- |
| `season_year` | |
| `team_id` / `team_name` | |
| `weeks` | weeks counted |
| `avg_weekly_efficiency` | mean of weekly `efficiency` |
| `total_points_left_on_bench` | sum of weekly `points_left` |
| `avg_points_left_per_week` | |
| `management_misses` | count of (week, player) where a benched, playable player outscored a started player they were eligible to replace |
| `worst_week` / `worst_week_points_left` | single week with the most points left on the bench |

Optionally also emit a per-team-per-week long CSV (`reports/lineup_efficiency_weekly.csv`)
for drilldowns.

---

## 2. Manager Power Rankings

**File:** `features/manager_power_rankings.py`
**Output:** `reports/manager_power_rankings.csv`

### Primitive: all-play win %

For each week, rank a team's score against *every* other team's score that week (not just
its scheduled opponent). This removes head-to-head schedule luck.

```
weekly_all_play_win% = teams_you_outscored_that_week / (league_size - 1)
season_all_play_win% = mean(weekly_all_play_win%) over weeks played
```

Ties count as half a win. Source: `build_matchups` weekly `home_score` / `away_score`
reshaped to one score per team per week (the same `_team_week_scores` reshape
`biggest_upset.py` already does).

### Composite score

Each component is converted to a league z-score, then weighted:

```
power_score = 0.40 * z(season_all_play_win%)
            + 0.25 * z(points_for_per_week)
            + 0.15 * z(actual_win%)
            + 0.10 * z(avg_weekly_efficiency)          # from feature 1
            + 0.10 * z(last_3_weeks_all_play_win%)     # recent form
```

Rank by `power_score` descending. Weights are a starting point — tilted toward all-play and
raw scoring because those predict future results better than actual record. If feature 1
isn't available in a given run, renormalize the remaining weights.

- `points_for_per_week`, `actual_win%`, `wins`, `losses`: available directly on `espn-api`
  `Team` objects (`points_for`, `wins`, `losses`, `standing`, `final_standing`) — no need to
  hand-sum from `matchups`.
- `actual_win%` = `wins / (wins + losses)`.

### Report — one row per team

| field | meaning |
| --- | --- |
| `rank` | 1 = strongest |
| `power_score` | composite |
| `all_play_win%` | schedule-independent record |
| `actual_win%` | real record |
| `luck` | `actual_win% - all_play_win%` (positive = lucky) |
| `points_for_per_week` | |
| `avg_weekly_efficiency` | from feature 1 (nullable) |
| `recent_form` | last-3-weeks all-play win% |
| `standing` | current ESPN standings position, for comparison |

---

## Wiring into `main.py`

Order, after the existing `build_matchups` block:

1. `players_weekly_df = build_players_weekly.build_players_weekly(current_league, 1, current_week)`
   — write CSV, optionally upsert DB.
2. `lineup_efficiency_df = lineup_efficiency.lineup_efficiency(players_weekly_df, output_dir=RESULTS_DIR)`
3. `manager_power_rankings.manager_power_rankings(build_matchups_df, current_league, lineup_efficiency_df, output_dir=RESULTS_DIR)`

Both assume ESPN H2H **points** scoring (same assumption as `build_matchups` /
`build_players_weekly`). Category scoring would need a separate branch — see
`docs/ideas.md` "Category-scoring support".

## Open questions

- Only count *completed* weeks — need a reliable "current completed week" cutoff (partial
  weeks would distort efficiency and all-play). `build_matchups` currently loops weeks 1-22
  blindly; may need to detect the last week with non-zero scores.
- `management_misses`: eligibility check is per-slot; a bench player who could only have
  replaced someone in a flex slot still counts. Fine for v1.
