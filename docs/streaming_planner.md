# Acquisition-aware weekly streaming plans

The CLI can now compare **sequences** of legal moves during the remaining
current matchup, including HOLD, delayed starts, and plans that leave acquisitions
unused. Python generates and scores the plans. Optional Claude review chooses
among supplied plans using dated context; it cannot create transactions or change
the points, budgets, player IDs, dates, or lineup assignments.

## Run

Offline replay (writes only to the requested output directory):

```bash
venv/bin/python -B main.py --mode waivers --snapshot examples/waiver_snapshot.json \
  --streaming-plan --max-moves 2 --output-dir /tmp/streaming-example
```

Live provider reads, with no database writes or roster transactions:

```bash
venv/bin/python -B main.py --mode waivers --team 3 --streaming-plan \
  --max-moves 3 --protect 12345 --output-dir reports/streaming
```

The team can also come from `ESPN_TEAM_ID`. Existing `--waiver-config`,
`--recent-window`, and `--protect` options apply. The generic `--basic-waivers`
board and default weekly reporting behavior are unchanged. Historical reconstructions
are not accepted: their acquisition usage was assumed, not observed.

Claude is **off by default**. To explicitly request one model comparison, add
`--claude-review --claude-model YOUR_MODEL_ID --streaming-context context.json`.
The adapter uses an already configured `ANTHROPIC_API_KEY`; a non-workspace-scoped
key may also require `ANTHROPIC_WORKSPACE_ID`. This feature does not configure
credentials. No live or paid Claude request was used for implementation validation.
Model availability and live authentication are unverified.

## Budgets, legality, and search bounds

- The provider adapter calculates matchup and season remaining allowances
  separately and keeps their minimum as `acquisitions_remaining`. All known
  allowances are decremented across the entire plan, never independently per move.
- A missing current-matchup usage entry is **unknown**, not zero. Missing finite
  daily-versus-matchup semantics also stops advice. Use verified explicit rule
  overrides when necessary. `null` means unlimited. The separate override fields
  are `matchup_acquisitions_remaining` and `season_acquisitions_remaining`.
  The planner refuses an explicit unknown separate allowance even if the combined
  allowance was overridden. Legacy snapshots without those fields enforce their
  combined allowance and disclose that limitation.
- Today's daily usage reduces today's capacity; subsequent dates reset the daily
  allowance. Every proposed acquisition is charged on its effective date. This
  first planner supports **SAME_DAY** timing only to keep transaction and lineup
  effective dates aligned. The existing single-move advisor still supports NEXT_DAY.
- The search supports at most **3 moves**, **14 calendar days**, **12 candidates**,
  and **8 retained states per move count per day**. Default depth is 2. Setting
  `--max-moves 0` produces HOLD only. Candidates are preselected by streaming PPG.
  It is a bounded beam search, not proof of global optimality.
- At most one move per day is considered, even in leagues permitting more. Only
  confirmed free agents with known availability times are eligible. No waiver
  claims, reacquisition of dropped players, IR moves, or future-matchup rollover.
- Each sequence is replayed through the daily lineup optimizer with current roster
  capacity, primary-position caps, eligibility, protected/undroppable players,
  injury availability, and roster/game locks. All points are recalculated.
- HOLD and lower-use plans remain available. Ranking prefers projected lineup gain,
  then fewer acquisitions. There is no invented point value for saving an acquisition.
  Future free-agent availability assumes nobody else acquires the player; refresh
  before any real decision. No transactions are submitted.
- Current injury availability is held constant in numerical forecasts. Context
  helps compare plans but does not introduce a new injury or opponent forecasting
  model. Protect core players explicitly; season VORP is comparison evidence,
  not an automatic protection rule.

## Dated context input

No automatic injury-news or opponent-history feed is added. The existing snapshot
provides opponents, game dates, and injury status. Additional evidence uses this
explicit interface (synthetic example; IDs must exist in the supplied snapshot):

```json
{
  "schema_version": 1,
  "league_id": 123,
  "season_year": 2026,
  "week": 12,
  "entries": [
    {
      "id": "injury-2",
      "kind": "injury",
      "player_id": 2,
      "observed_at": "2026-01-15T15:00:00Z",
      "source": "Synthetic team injury report",
      "summary": "Available but ankle soreness reported; no minutes limit verified."
    },
    {
      "id": "matchup-2",
      "kind": "matchup",
      "player_id": 2,
      "opponent": "BOS",
      "observed_at": "2026-01-15T15:00:00Z",
      "source": "Synthetic league-scored appearance sample",
      "summary": "Three prior appearances against BOS averaged 24 fantasy points.",
      "sample_games": 3,
      "window_start": "2025-01-01T00:00:00Z",
      "window_end": "2026-01-01T00:00:00Z"
    }
  ]
}
```

League, season ending year and matchup week must match the decision snapshot.
`observed_at` must describe the actual observation, not today's time assigned to an
old report. Freshness is measured against the decision snapshot: injury evidence
expires after 24 hours, matchup evidence after 7 days. Future observations and
opponents absent from the remaining schedule are excluded from review. Matchup
sample windows must end by the observation time. Small samples are descriptive,
not established causal matchup advantages. Sources and content are caller supplied
and not independently verified. Missing/stale evidence is exported explicitly.
Files are limited to 64 KiB / 100 entries; summaries and sources to 500 characters.

## Claude boundaries and fallback

The adapter sends at most eight available plans, retaining the best plan per
acquisition count including HOLD, relevant players' usable game dates/opponents,
season values, fresh evidence,
missing-context coverage, and limitations. It uses the
[Anthropic Messages API](https://platform.claude.com/docs/en/api/overview).
One request is allowed per invocation, with no retries or redirects, a 5-second
connect / 20-second read timeout, 16,000 UTF-8 input bytes including instructions,
900 output tokens, and a 32,000-byte response limit. The byte cap is a conservative
input-token bound, not a monetary cost estimate. Oversized inputs skip the model.

The model returns only a selected plan ID and structured comparison reasons with
player/evidence IDs. Unknown IDs, extra numeric fields, unsupported reasons,
unfresh citations, incomplete responses, malformed JSON, or invalid plans reject
the entire response. Python checks legality and recomputes plan numbers; explanatory
text uses fixed templates and attributed source excerpts. Arbitrary model prose is
not published as verified fact. No model-generated forecast adjustment is applied.
Missing key/model, errors, and invalid output retain the deterministic selection
with an explicit status. Secrets, raw HTTP bodies, and exception details are never
exported. Model access is not needed to use the planner.

## Exports and verification

`streaming_plans.csv` contains plans, actions, gains, counts, remaining allowances,
and a daily acquisition ledger,
selection and partial-data status. `streaming_calendar.csv` contains all retained
plans' daily lineups; its points reconcile to each plan's total.
`streaming_plan.json` includes the snapshot, search bounds, context coverage,
deterministic/model choices, and limitations. `streaming_snapshot.json` can be
replayed. `streaming_review.txt` explains the comparison/fallback.
Rank remains deterministic even when Claude selects a different plan; use the
`selected` column. Partial inputs never produce a definitive recommendation.
Replays preserve source decision time and player protection; pass the same
`--max-moves` and context file to reproduce the same search/review inputs.

Tests use synthetic league data and mocked model responses, including shared
budgets, missing usage, daily reset, locked drops, protected players, invalid
IDs/totals, stale context, fallback, and offline CLI export. They do not establish
live provider completeness, injury-news accuracy, or predictive performance.

```bash
venv/bin/python -B -m unittest discover -s tests -p 'test_streaming_planner.py'
venv/bin/python -B -m unittest discover -s tests
```
