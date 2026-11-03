# Frozen draft replay

`features/draft_replay.py` loads one frozen JSON bundle and reconstructs each
decision in pick order. It reports available alternatives, competition ranks,
value differences, and before/after roster slot coverage. It uses no ESPN,
database, credentials, current projections, injury sources, or model calls.

This answers what the supplied pre-draft inputs implied at each pick. It does
not score realized season production, assign an overall draft grade, or infer
the smartest manager from an undeclared scoring formula. The existing
`main.py --mode draft` current draft board and retrospective draft analysis are
separate workflows and retain their existing behavior.

## Run

From the repository root:

```sh
venv/bin/python -B -m features.draft_replay examples/draft_snapshot.synthetic.json
venv/bin/python -B -m features.draft_replay /path/to/frozen-draft.json --output /path/to/new-report.json
```

The first command prints JSON. The second creates a new file only after
validation and computation finish; it refuses to overwrite an existing file.
Its parent directory must already exist. Invalid inputs produce an error and
no report. A valid partial report exits successfully with `status: "partial"`
and machine-readable limitations. Consumers must inspect that status and the
per-pick coverage fields, not just the process exit code.

The included example uses invented players and forecast values adapted from
the research fixture's deterministic baseline. Its draft start, pool capture,
and scoring weights are synthetic assumptions for demonstrating the contract.
It is not a real league snapshot or proof of forecasting accuracy.

## Input contract version 1

Use the example as a structural template, replacing every synthetic fact with
verified inputs. Unknown JSON fields are rejected rather than silently ignored.
Duplicate JSON keys, nonfinite numbers and Boolean score values are rejected.
Input files are limited to 10 MiB. IDs can be strings or nonnegative integers;
integers normalize to strings, and collisions are rejected.

| Field | Contract |
| --- | --- |
| `schema_version` | Exactly `1`. |
| `snapshot_id`, `league_id`, `season_year` | Stable snapshot/league IDs and NBA season ending year. |
| `cutoff_at` | Required timezone-aware ISO timestamp. Every frozen capture must be strictly earlier. |
| `draft_started_at` | Timezone-aware timestamp later than the cutoff; null/missing blocks historical comparisons. |
| `teams` | Unique team IDs and optional display names. |
| `player_pool` | `complete`, `source`, `observed_at`, and `players`. Every player has a stable ID, optional name, and base `eligible_positions` or null. |
| `rankings` | One compatible source snapshot, described below; null/missing is allowed and stays unknown. |
| `rules` | `source`, `observed_at`, `scoring` and `roster`; unknown sections can be null. |
| `picks` | `complete` and `events` with `overall_pick`, `team_id`, `player_id`. |
| `keepers` | Explicit initial player/team assignments; `[]` means verified no keepers. Null/missing means unknown initial ownership. |
| `synthetic` | Optional Boolean label; set true for invented fixtures. |

The pool must include drafted/keeper players even when their forecast or
position is unknown. Add explicit placeholder rows for such players; do not
drop them. Unknown player/team references and duplicate selections are errors.
Player base positions are `PG`, `SG`, `SF`, `PF`, `C`; null means unknown and an
empty array means explicitly no starting-position eligibility.

`picks.complete: true` certifies that this file contains every selection from
pick 1 through its last listed pick. It does not claim the whole draft ended.
Complete logs must be contiguous. Events may arrive unsorted; replay sorts by
overall pick without inferring snake order or changing team ownership. Set
`complete: false` for uncertain logs or known gaps. This conservatively
suppresses availability comparisons throughout that file. Auction costs and
automatic/live draft synchronization are unsupported.

Keepers are already owned before pick 1 and must not also appear in the pick
events. If a provider represents keepers as consumed picks, normalize that
convention explicitly before creating this bundle; do not double-count them.

### Ranking and projection compatibility

`rankings` contains `metric`, `source`, `observed_at`, `league_id`, `season_year`,
`horizon`, `scoring_version`, and `entries`. An entry has `player_id` and `value`.
It may override source, capture time, league, season, horizon, or scoring
version; incompatible entries remain visible with null values and reasons.

- `projected_total_points`: higher finite full-season fantasy points are better.
  Genuine zero and negative values remain valid. Values are already scored
  under the supplied league rules; the engine does not invent projections or
  recompute totals from current stats.
- `pre_draft_rank`: lower positive integer source ranks are better. Differences
  are labeled source-rank positions, never projected points. All entries must
  match the parent source and capture instant, identifying one frozen board
  revision. Ranks from different boards or revisions are incomparable.

Only `full_season` horizons matching the bundle's league and season are usable.
`scoring_version` must equal the frozen rule version. Missing entries or values
stay null. A per-player override cannot rescue an incompatible parent snapshot.
Global source/capture/season/horizon/rule failures invalidate all its values.

The contract accepts contemporaneously captured inputs only. Source and
`observed_at` are declarations supplied by the caller; validation cannot prove
that a timestamp is authentic. Keep the original artifacts separately. A
forecast fetched now for an old season is not pre-draft evidence, and removing
current actuals cannot make it so. Later-retrieved historical archives need a
separate reviewed provenance contract and are not supported by version 1.

### Scoring and roster rules

`rules.scoring` requires `type: "H2H_POINTS"`, a nonempty `version`, and nonempty
numeric stat `weights`. The weights are recorded and validated; they are not
used to convert a different scoring system's already-applied fantasy totals.
Changing weights requires a new version and compatible frozen values.

`rules.roster` requires `starting_slots` and a nonnegative `bench_slots` count.
Starting slots can repeat and support `PG`, `SG`, `SF`, `PF`, `C`, `G`, `F`, `UT`,
`SG/SF`, `G/F`, `PF/C`, `F/C`. `UTIL` normalizes to `UT`; bench assignments render
as `BE`. Bench accepts any player. Empty starting slots with positive bench
capacity are allowed. IR eligibility and additional position/transaction caps
are not modeled: output describes slot feasibility only, not full ESPN roster
legality. Unsupported extra rule fields are rejected instead of being ignored.

The roster matcher maximizes occupied starting slots with unique players and
then fills bench slots. It can reassign a multi-position player to accommodate
a specialist. Each player counts once. This is unweighted slot coverage, not a
projection-optimized lineup or numerical roster-fit score. A bench addition can
have zero starting-slot change and still fit. Capacity overflow or a player
that cannot occupy a slot remains visible as `all_players_fit: false`.

## Output and missing coverage

The report preserves the input snapshot ID and a SHA-256 of canonical JSON
(sorted object keys, original array order). The hash identifies the supplied
bytes' logical JSON content; it does not verify historical authenticity.
`score_coverage` records normalized values, status and provenance. No current
data is fetched to fill a gap.

For each pick, the selected player is still available in the before-pick
comparison and disappears from subsequent available pools. Already drafted
players and keepers cannot appear as alternatives. Tied values share competition
rank; IDs determine display order only. All covered/uncovered available
alternatives remain visible when availability can be established.

`opportunity_gap_among_covered` equals best covered projected points minus
chosen points, or chosen source rank minus best covered source rank. A positive
number means the chosen value trails the best covered value. Zero may represent
a tie. Missing chosen values produce null gaps, not zero.

When the pool and pick history are complete but some values are missing,
`comparison_scope` is `covered_available`. The covered rank and gap are usable
only with that qualification; `selected_global_available_rank` remains null.
It becomes available only when every available player has a compatible value.

Incomplete/unverified pool history, incomplete picks, unknown keepers or missing
draft start block the available-player list, ranks, gaps and alternatives.
`observed_remaining_player_ids` is only the supplied pool minus known earlier
selections and must not be presented as a verified available set.

Roster output similarly distinguishes observed IDs from complete ownership.
Incomplete history makes capacity/slot consequences unknown. Missing frozen
positions suppress assignment claims; missing or late rules suppress roster
calculations. An `assignment` under incomplete ownership describes observed
players only, never a complete roster. `status` describes input coverage, not
whether a draft choice was good.

The first real report still requires a verified pre-draft full player pool,
compatible ranking/projection capture, exact draft start/cutoff, scoring and
roster rules in force, keeper state, and an ordered pick log. Current rankings,
realized season results and undated exports cannot substitute for these inputs.

## Validation

```sh
venv/bin/python -B -m unittest discover -s tests -p 'test_draft_replay.py'
venv/bin/python -B -m unittest discover -s tests
```

Tests cover temporal/scope gates, ties, zero/missing/negative values, rank-only
inputs, incomplete pools/logs, keepers, malformed IDs and events, unique slot
assignment, bench overflow, deterministic replay, input immutability, and CLI
output safety. These are implementation tests with synthetic data. Contextual
evidence ingestion, semantic claim checks, Claude integration and the research
pack's full model evaluation remain deferred.


### Explicit user assumption about forecast timing

Strict validation remains the default. For an explicitly requested sensitivity run:

```sh
venv/bin/python -B -m features.draft_replay input.json \
  --assume-forecasts-pre-draft --output new-assumed-report.json
```

The Python equivalent is `replay_draft(document, assume_forecasts_pre_draft=True)`.
This mode assumes only that supplied projected-total-point forecasts were available
pre-draft. It preserves actual `observed_at` values, labels the output
`user_assumed_forecasts_pre_draft`, records the assumption, and always keeps the
report partial rather than presenting timing as verified history. Ordinal boards
are not eligible for this override. Null cutoff/draft-start values remain unknown;
no substitute dates are generated. Malformed timestamps still fail validation.

The override does not certify rules, scoring compatibility, full player-pool
coverage, positions, pick completeness or keepers. Those gaps can still suppress
comparisons and roster fit. `score_coverage.observed_value` and each pick's
`observed_projection_value` expose supplied values even when incompatible;
`value` / `selected_value` remain null unless eligible for calculation. Observed
values must never be presented as validated decision scores.

### Captured drafted-cohort comparisons

`--assume-forecasts-pre-draft` also adds a separate
`captured_drafted_cohort_comparison` to each pick. This describes only captured
forecast values for that selection versus later selections in the supplied pick
log. It never calls those players available alternatives and excludes undrafted
players even when their values appear in the input pool.

Comparisons require matching source, actual capture instant, league, season,
full-season horizon and declared scoring version (including an explicitly
unknown version shared by both). Missing values, scope conflicts, missing
source/capture metadata and invalid parent scope remain excluded. Equivalent
UTC offsets refer to the same capture instant. Unknown historical scoring rules,
keeper state, full player pool, draft timing, positions and roster shape remain
unknown; they do not prevent this narrower descriptive arithmetic.

The result reports the highest compatible later captured value, all recorded
selections tied at that value, and `highest_later_captured_value_minus_selected`.
This signed difference may be positive, zero or negative. It is not an
opportunity cost, full available-player rank, roster-fit score or pick-quality
judgment. Counts show later recorded picks, comparable picks and excluded picks.
An incomplete log means only its observed records are compared; missing picks
are never inferred. The final recorded selection has no later comparator and
therefore a null difference. All true capture timestamps remain in
`score_coverage`; the result remains USER-ASSUMED and partial.

Existing availability, historical scoring and roster calculations retain their
original gates and fields. The strict default does not enable this cohort output.
`captured_drafted_cohort_summary` distinguishes raw forecast coverage, excluded
projections and picks with a descriptive comparison. No new provider/model
calls are made by the replay engine.
