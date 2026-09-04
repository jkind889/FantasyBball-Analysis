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

`games_remaining` is now supplied exactly by piece 3 in the `main.py` run
(`nba_schedule.attach_games_remaining`), falling back to the season-length
default only for players whose pro team can't be matched.

Wired into `main.py`: `projection_inputs.build_projection_inputs(current_league,
previous_league)` assembles the per-player input frame (current pace, prior-year
pace, ESPN projection) from the `League` roster objects, then the schedule is
attached and `project_rest_of_season` runs, writing `reports/projections.csv`.

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

This single number ranks a draft board and a waiver list. Wired into `main.py`
after the projection step, `league_size` from the current league, writing
`reports/player_value.csv`.

### 3. NBA schedule / games-remaining — `features/nba_schedule.py` (done, first pass)

ESPN already returns the full pro-team schedule on the `League` object
(`league.pro_schedule`). `build_nba_schedule()` flattens it to one row per
(team, game): `pro_team, opponent, is_home, game_date, scoring_period,
fantasy_week`, persisted to a `nba_schedule` table (DDL in
`CREATE_TABLE_SQL`, created on first run) and `reports/nba_schedule.csv`.

`fantasy_week` comes from `league.matchup_ids` when available, else 7-day
buckets (so it works pre-season before ESPN publishes played weeks).

Derives:

- `games_remaining(schedule_df, as_of)` -> `{pro_team: count}`; `attach_games_remaining()`
  maps it onto a players frame to feed piece 1 exactly.
- `games_per_week(schedule_df)` -> team x week grid — a 4-game week streamer is
  worth more than a better player on a 2-game week; the biggest in-season edge.

### 4. Entry points — `features/decision_modes.py` (done, first pass)

Decisions happen off-cycle from the weekly retrospective run, so `main.py`
takes a `--mode`:

    python main.py                     # weekly: full retrospective + predictive run (default)
    python main.py --mode draft
    python main.py --mode waivers [--week N]
    python main.py --mode startsit [--week N] [--team TEAM_ID]

A decision mode skips all the retrospective work and the MySQL writes: it runs
only the shared pipeline (`features/predictive_engine.run_projection_pipeline` —
schedule -> games remaining -> ROS projection -> VORP), turns it into one table,
prints the top 20, and writes a CSV. The scoring functions are pure DataFrame
code with unit tests; `decision_modes.run()` is the only part that touches the
`League` object.

**`--mode draft` -> `reports/draft_board.csv`**
The projected-VORP board for the whole player pool (every rostered player plus
~500 free agents), ranked by `vorp_ros`. ESPN's own view rides alongside for
comparison — `espn_proj_avg_points`, `replacement_pts_per_game`,
`prior_avg_points` — so you can see where the projection disagrees with the
crowd. (True ADP isn't exposed by `espn-api`; the free-agent pull is already
ADP-sorted, and `espn_proj_avg_points` is the stand-in until ADP is scraped.)

**`--mode waivers` -> `reports/waiver_board.csv`**
Free agents only, with two rankings side by side:
- `ros_rank` / `vorp_ros` — season-long value, for a roster spot you'll keep.
- `next_week_rank` / `next_week_value` — `proj_pts_per_game x games in that
  fantasy week` (from `games_per_week`), for a stream. A 4-game-week nobody can
  outrank a 2-game-week starter here.
`--week` picks the fantasy week (default: current).

**`--mode startsit` -> `reports/start_sit.csv`**
Projects every player on your roster for the target week
(`proj_pts_per_game x games_this_week`), drops anyone `OUT`/suspended, then runs
a `scipy.optimize.linear_sum_assignment` over the starting slots (`PG SG SF PF
C G F UTIL UTIL UTIL`) against each player's ESPN `eligibleSlots`. Output is one
row per player with `recommended_slot` (a slot name or `BENCH`), starters first.
Needs your team id: `--team` or the `ESPN_TEAM_ID` env var.

## Downstream features (backlog, build on 1–3)

- waiver-wire add/drop pair recommendations
- streaming optimizer over a scoring period
- injury replacement finder (shares an `eligible_slot` with a ruled-OUT player)
- trade evaluator (value both sides by ROS VORP + roster need)
- mock-draft simulator (opponents pick near ADP; Monte Carlo your pick)
