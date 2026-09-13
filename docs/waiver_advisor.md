# Waiver add/drop advisor

The roster-aware advisor compares one add/drop move at a time against holding
your current roster. It uses recent fantasy production and the games your
lineup can actually use during the remaining matchup. It does not submit ESPN
transactions.

## Run with live league data

```bash
venv/bin/python main.py --mode waivers --team 3
venv/bin/python main.py --mode waivers --team 3 --recent-window 5 --protect 12345
```

Replace the example team/player IDs with your own. `ESPN_TEAM_ID` can supply the
team; repeat `--protect` for additional players. With no selected team, the
original generic waiver board still runs. To explicitly request that board even
when `ESPN_TEAM_ID` is set:

```bash
venv/bin/python main.py --mode waivers --basic-waivers --week 5
```

Roster-aware advice currently supports the **current matchup** in daily
head-to-head points leagues, individual-game lineup locks, and individual-game
or first-game-of-day roster locks. It reads actual starting/bench capacities,
primary-position limits, droppability, availability, and acquisition limits.
IR players remain on IR; resolving invalid IR configurations and automatic IR
moves are outside this release. Games/starts caps and finite lineup-move limits
require a different optimizer and are rejected explicitly.

An ended season stops with an instruction to select the active `ESPN_YEAR` or
use a snapshot. As of the September 13 implementation check, the configured
2026 league was past its final scoring period. Do not mistake an offseason
empty schedule for a live no-move recommendation.

## Recent form

The default window is six completed appearances, configurable to five:

```text
recent_form = (recent mean + recent median) / 2
recent_weight = 0.60 × fresh appearances / selected window
streaming projection = recent_weight × recent_form
                     + (1 - recent_weight) × existing baseline projection
```

Only appearances within 21 days of the snapshot enter the blend. Fewer than
three fresh games, unavailable/incomplete logs, or conflicting duplicates cause
a labeled baseline-only fallback. Missing baseline projections remain unknown.
Real zero-point appearances count; DNPs, future games, projected stats, and
uncompleted games do not. Per-game ESPN `appliedTotal` uses your league's scoring,
so “30 points” means fantasy points, not basketball scoring points.

The mean/median blend reduces the influence of one outlier. With full recent
windows, a player with a 25-point baseline and 34-point recent mean/median gets
a 30.4-point streaming estimate. A player with a 30-point baseline and recent
20-point production gets 24.0. With equal schedules and fit, the hot player is
more valuable for this matchup. The season projection and draft rankings are
not changed by this short-term calculation.

Both your current roster and every candidate use the same adjustment. The
advisor optimizes each day, then ranks moves by the difference in total projected
lineup points. It does not count completed games, add games that miss a waiver
deadline, or credit a player who cannot displace a stronger legal starter.
In-progress/locked assignments reserve their existing slots but have zero
contribution to the *new decision* comparison. Current injury statuses are held
constant as an explicit scenario assumption through the remaining matchup.

## Read the outputs

| File in `reports/` | Contents |
| --- | --- |
| `waiver_recommendations.csv` | Ranked moves and hold, projected gain, effective time, usable starts, recent-form evidence, season-value tradeoffs, limitations |
| `remaining_week_calendar.csv` | Player/day assignments for hold and the first 20 ranked move scenarios; join on `action_id` |
| `recent_form.csv` | Each player's baseline, last-five/six averages, mean/median blend, sample size, threshold counts, applied weight and fallback |
| `waiver_rejected.csv` | Excluded actions and reasons, including unknown acquisition times or no verified legal drop |
| `waiver_snapshot.json` | Normalized input state, rules, game logs, schedule, and `as_of`; no credentials or raw ESPN responses |

`recommended=true` identifies a positive-gain confirmed available move, or hold
when no such move improves the lineup. `conditional` means a waiver claim still
has to be awarded. `partial` marks comparisons with missing evidence; they can
be inspected but are not presented as definitive recommendations. Unknown
availability/droppability prevents a move from being proposed; inspect the
rejection export before interpreting hold as a conclusion about the entire pool.
The console shows a compact selection of columns; CSVs contain the full evidence.

Calendar points sum to each scenario's `candidate_points`. Subtract the hold
total to reconstruct `net_gain`. The calendar is an audit CSV in this release;
an interactive calendar/dashboard is still future work. All candidate pairs
remain in the recommendations CSV even when only the first 20 have calendar rows.

## Replay the included example without network or MySQL

```bash
venv/bin/python main.py --mode waivers --snapshot examples/waiver_snapshot.json
venv/bin/python main.py --mode waivers --snapshot examples/waiver_snapshot.json --recent-window 5
```

This synthetic example has one roster spot and three equal-schedule guards.
The expected recommendation adds **Hot free agent** and drops **Cold incumbent**,
for **+19.2 projected points over three usable starts**. Replay writes the usual
reports; use a separate working directory if you want to preserve older output
files. Save copies of snapshots before overwriting them to build a history.
Replay preserves the saved recent window and protected players unless a new
window is explicitly requested. It uses its stored `as_of`, not today's roster or later outcomes. It is not
an as-of historical ESPN query. Exact backtesting requires snapshots saved at
the original decision time; a reconstructed real-data demo is also available.

## Previous-season demo with real players

Fetch a historical scenario for **ybgcorp (team 9)** in the **2025–26 season**:

```bash
venv/bin/python main.py --mode waivers --historical-date 2026-01-08 --season 2026 --team 9
```

ESPN uses the ending year, so `--season 2026` selects 2025–26. This command does
not change `ESPN_YEAR` in `.env`. Outputs go into a separate directory:
`reports/historical/2026/team-9/2026-01-08/`.

A real-data run is included there: [view the ybgcorp demo](../reports/historical/2026/team-9/2026-01-08/historical_demo.md).
Replay it without network or MySQL:

```bash
venv/bin/python main.py --mode waivers --snapshot reports/historical/2026/team-9/2026-01-08/waiver_snapshot.json
```

Use `--recent-window 5` to compare a five-game window, `--protect PLAYER_ID` to
keep a core player out of drop scenarios, or `--output-dir /path/to/demo` to
save a variant separately. The summary includes a comparison of projected gains
and realized points from the fixed planned lineups where actuals are available.
The separate `historical_actuals.json` supplies evaluation data on replay; it is
never an input to the rankings. Shared missing actuals cancel in the incremental
comparison, but missing changed games leave the gain unknown.

The reconstruction uses the previous scoring day's archived roster and excludes
players owned by other teams on that day. It computes the baseline from completed
appearances strictly **before the selected date**, then applies the same recent-form
blend and daily lineup optimizer. It does not reuse final-season averages or later
ESPN projections. Players without any pre-cutoff appearances are excluded from the
inferred candidate pool.

This demonstrates the feature, rather than establishing its historical accuracy:

- Historical waiver processing, transaction usage, and exact injury eligibility
  are not fully recovered. The scenario assumes one acquisition, immediate
  availability, droppable non-IR players, and playable non-IR players.
- NBA team is inferred from prior appearances; trades before the cutoff without
  a subsequent appearance can make that inference stale.
- The archived roster is from the previous day, and eligibility, rules, and the
  actual schedule may reflect later changes.
- Season-to-date means replace the live prior/current/ESPN baseline blend.
- Later actual results score the already-chosen lineups; they do not select or
  re-optimize the recommendations.

These rows are marked `analysis_mode=historical_demo`, `status=partial`, and
`recommended=false` so they cannot be mistaken for verified live opportunities.
The short-term comparison can suggest dropping a star with fewer usable games;
protect your core roster when exploring streaming moves, since this demo does
not estimate that player's long-term replacement cost.

## Rules and explicit overrides

Some required rules or player fields may be absent from an ESPN response. The
advisor identifies missing rules instead of substituting a standard league.
Use a JSON file with `--waiver-config path/to/waivers.json` when needed.

Example structure; these values describe an example league and must match your
actual settings before using them:

```json
{
  "timezone": "America/New_York",
  "rules": {
    "acquisition_timing": "SAME_DAY",
    "drop_lock": "FIRSTGAME_SCORINGPERIOD",
    "games_cap": null,
    "lineup_move_limit": null,
    "acquisitions_remaining": null,
    "daily_acquisition_limit": 1,
    "daily_acquisitions_used": 0,
    "ir_legal": true
  },
  "players": {
    "12345": {"protected": true},
    "67890": {"availability": "WAIVERS", "available_at": "2026-11-05T08:00:00-05:00"}
  }
}
```

For acquisition counts, `null` means unlimited, not unknown. Daily limits and
matchup/season limits are tracked separately. A used-up daily allowance shifts
the modeled move to tomorrow; a used-up matchup/season allowance rejects it.
Future daily capacity assumes no intervening acquisitions, since each scenario
contains only one proposed transaction. First-game roster locks also shift a
late action to the next day even when the selected player's game starts later.

Player overrides support `protected`, `undroppable`, `availability`, and
`available_at`. Unknown waiver processing time is not inferred from a generic
waiver duration. If needed, `matchup_scoring_periods` can explicitly list the
current matchup's **daily scoring-period IDs**. ESPN's settings `matchupPeriods`
contains week IDs and must not be used as that list. Without a verifiable full
target-period boundary, advice is marked partial.

The JSON snapshot format is versioned (`schema_version: 1`); the included example
is also a complete reference for offline inputs. Live rule/player overrides are
recorded as a limitation in the resulting snapshot. Snapshot replay uses those
stored rules and cannot be combined with a new live config.

## Verification

```bash
venv/bin/python -m unittest discover -s tests
```

Tests exercise hot/cold streaks, outliers, small/stale samples, DNPs, duplicate
logs, actual ESPN per-game split normalization, positional congestion, locked
slots, delayed claims, daily and matchup limits, partial data, and offline CLI
replay. Small lineup fixtures are checked against exhaustive assignment.
The initial recency weights are product defaults; they have not yet been tuned
against held-out historical prediction outcomes.
