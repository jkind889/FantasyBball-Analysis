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

### Weekly Player Builder

`features/build_players_weekly.py` builds weekly player lineup rows (points, lineup slot, started/benched, injury status) from ESPN box scores. It is available for other features to use but is not yet wired into `main.py`'s output.

### Weekly Email Digest

`features/build_digest.py` builds a short text summary of the latest run — closest matchup, biggest blowout, top riser, and best draft value pick — and, if `alerts/send_email.py` sends it via Gmail SMTP.

The digest is sent automatically at the end of `main.py` when `ALERT_EMAIL_FROM`, `ALERT_EMAIL_TO`, and `ALERT_EMAIL_APP_PASSWORD` are set in `.env`. If any of these are missing, `main.py` skips the email and continues (the digest step never blocks the rest of the run).

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
```

`ALERT_EMAIL_*` are optional — omit them to skip the weekly email digest. `ALERT_EMAIL_APP_PASSWORD` must be a Gmail [App Password](https://support.google.com/accounts/answer/185833), not your regular account password.

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
    matchup_id INT AUTO_INCREMENT PRIMARY KEY,
    season_year INT NOT NULL,
    week INT NOT NULL,
    home_team_id INT NOT NULL,
    away_team_id INT NOT NULL,
    home_score DECIMAL(8,2) NOT NULL,
    away_score DECIMAL(8,2) NOT NULL,
    winner_team_id INT NULL,
    margin DECIMAL(8,2) NOT NULL,
    UNIQUE KEY uniq_matchup (season_year, week, home_team_id, away_team_id)
);
```

## Run

```bash
venv/bin/python main.py
```

After a successful run, check `data/` for source snapshots and `reports/` for analysis outputs.

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
