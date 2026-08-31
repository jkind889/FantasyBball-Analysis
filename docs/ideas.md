# Future Ideas / Roadmap

Backlog of scaling ideas and new features, captured 2026-08-27, updated 2026-08-28. Not scheduled — pull from here when picking up new work.

## Done

- **Wire up matchups output** — `build_matchups` now writes `reports/matchups.csv` and upserts into a `matchups` MySQL table (see `main.py`, `features/build_matchups.py`).
- **Alerts / digest** — `features/build_digest.py` + `alerts/send_email.py` send a weekly email digest (closest matchup, blowout, upset of the week, top riser, best draft value) from `main.py`, gated on `ALERT_EMAIL_*` env vars.
- **Automate the run** — weekly `launchd` scheduling via `scripts/run_main.sh` + `com.fantasybball-analysis.weekly.plist` (GitHub Actions was ruled out — the MySQL DB is local-only and unreachable from a hosted runner).
- **Finish `features/biggest_upset.py`** — an upset is a win despite a lower season-average score, computed from weeks played *before* that matchup only, ranked by average-score gap. Outputs `reports/biggest_upsets.csv` and feeds an "Upset of the week" line into the email digest.

## Low effort, uses what's already built

- **Wire up weekly players output** — `features/build_players_weekly.py` exists and is fully built but is never called from `main.py`. Same treatment: CSV/DB output.
- **Manager power rankings** — combine weekly wins, points-for/against, and roster efficiency (started vs. bench points) from `players_weekly` into a single ranking report. Note: `Team` objects from `espn-api` already expose `points_for`/`points_against`/`standing`/`final_standing` directly, so a power-rankings report may not even need to hand-sum from `matchups`.

## Medium — new analysis features (same pattern as existing `features/*.py`)

- **Best/worst waiver pickups** — compare a player's points-per-game before/after being added off waivers vs. while rostered elsewhere. Data source confirmed: `League.recent_activity(size=25, msg_type='FA'|'WAVIER'|'TRADED')` (requires auth, already have `espn_s2`/`swid`) returns `Activity` objects — `date` (epoch ms) + `actions`: list of `(team, action, player, bid_amount)` tuples.
- **Trade analyzer** — same `recent_activity(msg_type='TRADED')` feed as above; evaluate trade value similarly to draft value (points gained/lost vs. expectation per player involved).
- **Positional scarcity / draft strategy report** — expected points by position and round, to see which positions get drafted too early/late league-wide.
- **Lineup efficiency** — % of available points left on the bench per team per week, using the `was_started` field already tracked in `build_players_weekly`.
- **Injury impact tracking** — using `injury_status`/`injured` fields already pulled, quantify points lost to injury per team.
- **Roster-source tagging** — `Player.acquisitionType` (draft vs. FA vs. trade) is available from the API but not currently captured anywhere; would make roster-source filtering exact instead of inferred.

## Scaling / architecture

- **Multi-season history** — schema is already season-keyed (`season_year` on every table), so a historical trends dashboard (all-time win%, draft grade by year) becomes viable once 2+ seasons are banked.
- **Multi-league support** — parametrize `ESPN_LEAGUE_ID` so the same pipeline can run for more than one league, tagging rows by `league_id`.
- **Dashboard** — lightweight Streamlit or static HTML page reading from MySQL/CSVs, instead of opening CSVs by hand.
- **Category-scoring support** — `build_matchups`/`build_players_weekly` currently assume ESPN's H2H *points* scoring format (`home_score`/`away_score`). The API also supports an H2H *category* format (`home_wins`/`home_stats` per-stat dict instead of a single score) — only relevant if this league (or a future one added under multi-league support) uses category scoring; would need a branch in those builders.

## Reference

- ESPN API docs: https://github.com/cwendt94/espn-api/wiki — Basketball League/Team/Player/BoxScore/Activity class pages cover the methods/fields noted above.
