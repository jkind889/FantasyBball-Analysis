# Predictive / decision-support engine

The existing `features/*` are retrospective — they score what already happened
(VOPE, projection accuracy, lineup efficiency). The predictive engine points the
same math *forward*: value a player by what they will produce from the decision
point onward, so the project can help **before the draft** (who to pick) and
**during the season** (who to add).

Everything is built from data we already pull from `espn-api` plus one genuinely
new source (the NBA team schedule). Modules follow the existing pattern: a pure
function that takes DataFrames, optionally writes a CSV to `output_dir`, and is
unit-tested without network access.

## Pieces

### 1. Rest-of-season projection — `features/projections.py` (done, first pass)

`project_rest_of_season(players_df, ...)` blends, per player:

- prior-season points-per-game (`player_season` table, previous year)
- current-season points-per-game so far
- ESPN's own projection (`projected_avg_points`)

weighted by how many games the player has logged this season. Preseason
(0 GP) leans entirely on priors + ESPN; by mid-season the current-season pace
dominates (`STABILIZE_GP`, `MAX_CURRENT_WEIGHT`).

Output: `proj_pts_per_game`, `games_remaining`, `ros_total`, plus `basis`
(`preseason` / `blended` / `current`) and `current_weight` for transparency.

`games_remaining` is an input for now (caller passes an estimate); it becomes
exact once piece 3 lands.

### 2. Player value / VORP — `features/player_value.py` (done, first pass)

`compute_player_value(projections_df, roster_slots, league_size, ...)`:

- **replacement level** = projected per-game production of the (roughly)
  last startable player at each position, given league size and the starting
  lineup template.
- `vorp_per_game = proj_pts_per_game - replacement_pts_per_game`
- `vorp_ros = vorp_per_game * games_remaining`

Position eligibility is parsed from the `position` string (`"PG/SG"` ->
`{PG, SG, G, UTIL}`). Both a global and a per-position replacement level are
produced so positional scarcity is visible.

This single number ranks a draft board and a waiver list.

### 3. NBA schedule / games-remaining — `features/nba_schedule.py` (TODO)

The one new data source. A `nba_schedule` table (pro_team, game_date,
fantasy_week). Derives:

- `games_remaining` per player (feeds piece 1 exactly)
- games **per fantasy week** per team — a 4-game week streamer is worth more
  than a better player on a 2-game week; this is the biggest in-season edge.

### 4. Entry points (TODO)

Decisions happen off-cycle from the weekly retrospective run, so add modes:

- `python main.py --mode draft` — projected VORP board vs. ESPN ADP
- `python main.py --mode waivers` — free agents ranked by ROS VORP and by
  next-week value
- `python main.py --mode startsit` — project the upcoming week, run the
  `lineup_efficiency` assignment solver forward for a recommended lineup

## Downstream features (backlog, build on 1–3)

- waiver-wire add/drop pair recommendations
- streaming optimizer over a scoring period
- injury replacement finder (shares an `eligible_slot` with a ruled-OUT player)
- trade evaluator (value both sides by ROS VORP + roster need)
- mock-draft simulator (opponents pick near ADP; Monte Carlo your pick)
