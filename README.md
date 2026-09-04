# FantasyBball-Analysis

FantasyBball-Analysis is a Python reporting script for an ESPN fantasy basketball league. It pulls league, roster, draft, player-season, and matchup data from `espn-api`, stores the core league data in MySQL, and writes CSV reports for draft value, projection accuracy, roster movement, and breakout-player analysis.

## What It Does

The main script in `main.py` performs one end-to-end run:

1. Loads ESPN and MySQL credentials from `.env`.
2. Connects to the current ESPN fantasy basketball season and the previous season.
3. Upserts teams, drafted players, final rosters, and player-season totals into MySQL.
4. Reads the database back into analysis DataFrames.
5. Generates CSV snapshots in `data/` and report files in `reports/`.

The project is now mostly database-first: ESPN is used to refresh the database and as a fallback for players missing from the cached/stat tables.

## Features

### Draft Value Analysis

`features/draft_analysis.py` compares each drafted player's actual fantasy production against the average production of players drafted in the same five-pick bucket.

Outputs:

- `reports/draft_analysis.csv`
- `reports/draft_analysis_by_team.csv`

Key fields:

- `draft_bucket`: five-pick draft range, such as `1-5` or `46-50`.
- `expected_total_points`: average eligible total points for the player's draft bucket.
- `vope_score`: actual total points minus expected total points.
- `vope_percentile`: percentile rank of the VOPE score.
- `value_status`: `Elite Steal`, `Steal`, `Fair`, `Bust`, `Major Bust`, `Insufficient GP`, or `Missing Stats`.

The current draft-analysis minimum is `21` games played.

### Projection Analysis

`features/projection_analysis.py` compares actual player totals against projected totals and labels each player by projection performance.

Output:

- `reports/projection_analysis.csv`

Key fields:

- `roster_source`: whether the player appeared in the draft, final roster, or both.
- `projection_difference`: actual total points minus projected total points.
- `percent_above_projection`: projection difference as a percentage of projected points.
- `projection_status`: `Far Above Projection`, `Above Projection`, `Near Projection`, `Below Projection`, or `Far Below Projection`.

### Roster Points Comparison

`features/roster_points_comparison.py` compares each fantasy team's drafted-player point total with its final-roster point total.

Output:

- `reports/roster_points_comparison.csv`

Key fields:

- `draft_roster_total_points`
- `final_roster_total_points`
- `point_difference`

This helps show which teams gained or lost production after draft day through roster changes.

### Breakout Player Analysis

`features/build_breakout_players.py` ranks players by year-over-year average fantasy point growth.

Output:

- `reports/breakout_players.csv`

Key fields:

- `current_avg_points`
- `previous_avg_points`
- `avg_points_jump`
- `current_games_played`
- `previous_games_played`
- `source_previous_season`

The breakout workflow uses `cache.py` to look up previous-season player rows from the database first. If a player is missing from the database and a previous ESPN league object is available, the cache can fall back to previous-season free agents by position and convert ESPN player objects into dictionary rows.

### Matchup Analysis

`features/build_matchups.py` builds one row per matchup per week from ESPN box scores, for weeks `1` through `22`.

Outputs:

- `reports/matchups.csv`
- `matchups` MySQL table (upserted each run, keyed on `season_year`, `week`, `home_team_id`, `away_team_id`)

Key fields:

- `winner_team_id` / `loser_team_id`: `NULL` on a tie.
- `margin`: absolute point difference between the two teams.

### Biggest Upset Analysis

`features/biggest_upset.py` finds matchups where a team won despite having a lower season average score than its opponent, using each team's average from weeks played *before* that matchup only (not full-season, so a blowout can't inflate its own baseline).

Output:

- `reports/biggest_upsets.csv`

Key fields:

- `winner_season_avg_entering_week` / `loser_season_avg_entering_week`: each team's average score from prior weeks only.
- `avg_gap`: `loser_season_avg_entering_week - winner_season_avg_entering_week` — how the report is ranked, biggest gap first.

A team's first matchup of the season has no prior average and is excluded, as are ties.

### Weekly Player Builder

`features/build_players_weekly.py` builds weekly player lineup rows (points, lineup slot, started/benched, injury status) from ESPN box scores. `main.py` runs it for weeks `1` through the league's current matchup period and writes `reports/players_weekly.csv`. It also feeds the lineup-efficiency and power-rankings features.

### Lineup Efficiency

`features/lineup_efficiency.py` measures how well each manager set their lineup: actual started points vs. the best legal lineup they could have started from the same rostered players that week.

Outputs:

- `reports/lineup_efficiency.csv` (one row per team per season)
- `reports/lineup_efficiency_weekly.csv` (one row per team per week)

For each team-week, the optimal lineup is a max-weight assignment of rostered players to that week's starting slots (`scipy.optimize.linear_sum_assignment`), respecting each player's `eligible_slots`. Players who were `OUT` (or injured and suspended) are excluded from the optimal lineup, since the manager couldn't have started them. `IR`-slot players are excluded from the starting pool.

Key fields:

- `avg_weekly_efficiency`: mean of weekly `actual / optimal`.
- `total_points_left_on_bench`: season sum of `optimal - actual`.
- `management_misses`: weeks a benched, playable player outscored a starter they were eligible to replace.
- `worst_week` / `worst_week_points_left`: the single week with the most points left on the bench.

### Manager Power Rankings

`features/manager_power_rankings.py` ranks managers by a composite z-score, not just their record.

Output:

- `reports/manager_power_rankings.csv`

The core input is **all-play win %** — each week, a team's score is ranked against every other team's score that week, not just its scheduled opponent, which removes head-to-head schedule luck. The composite is:

```
power_score = 0.40 * z(all_play_win_pct)
            + 0.25 * z(points_for_per_week)
            + 0.15 * z(actual_win_pct)
            + 0.10 * z(avg_weekly_efficiency)
            + 0.10 * z(recent_form)          # last 3 weeks all-play
```

If lineup efficiency is unavailable, its weight is redistributed across the other terms.

Key fields:

- `all_play_win_pct` / `actual_win_pct`
- `luck`: `actual_win_pct - all_play_win_pct` (positive = lucky).
- `recent_form`: last-3-weeks all-play win %.
- `standing`: current ESPN standings position, for comparison.

### Weekly Email Digest

`features/build_digest.py` builds a short text summary of the latest run — closest matchup, biggest blowout, upset of the week, top riser, and best draft value pick — and, if `alerts/send_email.py` sends it via Gmail SMTP.

The digest is sent automatically at the end of `main.py` when `ALERT_EMAIL_FROM`, `ALERT_EMAIL_TO`, and `ALERT_EMAIL_APP_PASSWORD` are set in `.env`. If any of these are missing, `main.py` skips the email and continues (the digest step never blocks the rest of the run).

### Predictive Engine (rest-of-season projection -> player value)

Points the same math forward to help *before* the draft and *during* the season. Every `main.py` run also produces:

- `reports/projections.csv` — `features/projections.py` blends prior-season pace, current-season pace, and ESPN's projection into `proj_pts_per_game`, weighted by how many games a player has logged this season, then extends it over `games_remaining` (exact, from the NBA schedule) for a `ros_total`.
- `reports/player_value.csv` — `features/player_value.py` turns those projections into `vorp_ros` (value over replacement): `proj_pts_per_game` minus the projected rate of the last startable player at the position, times games remaining. One number that ranks a draft board and a waiver list the same way.
- `reports/nba_schedule.csv` + `nba_schedule` table — `features/nba_schedule.py` flattens ESPN's pro-team schedule to one row per team per game, and derives games remaining and a team-by-fantasy-week game grid.

Full design: `docs/predictive_engine.md`.

### Decision Modes

`main.py --mode <mode>` runs a single forward-looking report instead of the full weekly pipeline. Decision modes skip the retrospective analysis and all MySQL writes — they only need ESPN and `scipy`.

| Command | Output | What it answers |
| --- | --- | --- |
| `python main.py` (default) | all reports | the full weekly retrospective + predictive run |
| `python main.py --mode draft` | `reports/draft_board.csv` | who has the most projected value — the whole pool (rosters + free agents) ranked by rest-of-season VORP, with ESPN's own projection alongside |
| `python main.py --mode waivers [--week N]` | `reports/waiver_board.csv` | who to pick up — free agents ranked two ways: `ros_rank` (season-long value) and `next_week_rank` (`projection x games that fantasy week`, for streamers) |
| `python main.py --mode startsit [--week N] [--team ID]` | `reports/start_sit.csv` | who to start — projects your roster for the week, drops `OUT` players, and solves the best legal lineup (`recommended_slot` per player) |

`--mode startsit` needs your team id, from `--team` or the `ESPN_TEAM_ID` env var. `--week` defaults to the current fantasy week.

The scoring logic lives in `features/decision_modes.py` (pure, unit-tested); the shared projection chain is `features/predictive_engine.py`.

## Generated Files

### `data/`

- `data/players.csv`: database-backed snapshot of current final rosters and player-season stats.
- `data/draft.csv`: database-backed snapshot of draft picks.

### `reports/`

- `reports/draft_analysis.csv`
- `reports/draft_analysis_by_team.csv`
- `reports/projection_analysis.csv`
- `reports/roster_points_comparison.csv`
- `reports/breakout_players.csv`
- `reports/matchups.csv`
- `reports/biggest_upsets.csv`
- `reports/players_weekly.csv`
- `reports/lineup_efficiency.csv`
- `reports/lineup_efficiency_weekly.csv`
- `reports/manager_power_rankings.csv`
- `reports/projections.csv`
- `reports/player_value.csv`
- `reports/nba_schedule.csv`
- `reports/draft_board.csv` (only from `--mode draft`)
- `reports/waiver_board.csv` (only from `--mode waivers`)
- `reports/start_sit.csv` (only from `--mode startsit`)

## Setup

Install dependencies:

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

Create a `.env` file using `.env.example` as the starting point:

```bash
ESPN_LEAGUE_ID=608130406
ESPN_YEAR=2026
ESPN_SWID={YOUR_SWID}
ESPN_S2=YOUR_ESPN_S2
DB_HOST=localhost
DB_PORT=3306
DB_NAME=fantasy_basketball
DB_USER=root
DB_PASSWORD=your_password
ALERT_EMAIL_FROM=your_gmail_address@gmail.com
ALERT_EMAIL_TO=your_gmail_address@gmail.com
ALERT_EMAIL_APP_PASSWORD=your_gmail_app_password
ESPN_TEAM_ID=
```

`ESPN_TEAM_ID` is optional — only `python main.py --mode startsit` uses it (your team id in the league). `ALERT_EMAIL_*` are optional — omit them to skip the weekly email digest. `ALERT_EMAIL_APP_PASSWORD` must be a Gmail [App Password](https://support.google.com/accounts/answer/185833), not your regular account password.

The script expects a MySQL database with tables for:

- `teams`
- `players`
- `draft_picks`
- `player_season`
- `final_rosters`
- `matchups`

Create the `matchups` table with:

```sql
CREATE TABLE matchups (
    season_year INT NOT NULL,
    week INT NOT NULL,
    home_team_id INT NOT NULL,
    away_team_id INT NOT NULL,
    home_score FLOAT,
    away_score FLOAT,
    PRIMARY KEY (season_year, week, home_team_id, away_team_id)
);
```

`winner_team_id` and `margin` are not stored in the database — they're derived in-memory each run (in `features/build_matchups.py`) for the CSV report, the email digest, and `features/biggest_upset.py`, since they're always cheap to recompute from `home_score`/`away_score`.

## Run

```bash
venv/bin/python main.py
```

After a successful run, check `data/` for source snapshots and `reports/` for analysis outputs.

For a single forward-looking report without the full pipeline:

```bash
venv/bin/python main.py --mode draft
venv/bin/python main.py --mode waivers --week 5
venv/bin/python main.py --mode startsit --team 3
```

See [Decision Modes](#decision-modes) above.

### Weekly scheduling (macOS `launchd`)

Since MySQL runs locally, this project schedules `main.py` with `launchd` rather than a cloud CI cron (GitHub Actions runners can't reach `localhost`).

1. `scripts/run_main.sh` runs `main.py` with the venv's Python and logs output to `logs/main.log`.
2. `com.fantasybball-analysis.weekly.plist` is a `launchd` agent template that calls the script weekly (Sunday nights by default). Copy it into `~/Library/LaunchAgents/` and edit the `<string>` paths to match your local repo location before loading it:

```bash
cp com.fantasybball-analysis.weekly.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.fantasybball-analysis.weekly.plist
```

To stop it:

```bash
launchctl unload ~/Library/LaunchAgents/com.fantasybball-analysis.weekly.plist
```

Note that `launchd` only runs the job if your Mac is on and awake at the scheduled time — it does not run in the cloud.

## Tests

Run the full unit test suite:

```bash
venv/bin/python -m unittest discover -s tests
```
