# Provider projections and retrospective comparisons

`reports/projection_analysis.csv` compares actual season-to-date points with an
observed ESPN full-season forecast. It is a descriptive comparison, not proof of
preseason forecasting accuracy. A forecast retrieved after a season cannot be
represented as a forecast known before that season. During an unfinished season,
the actual total and the full-season target cover different elapsed time; the
signed difference is progress against the target, not a final forecast error.

## Why zeros appeared

The old SQL supplied literal zero projections. The newer local branch replaced
those with `Player.projected_total_points` / `projected_avg_points`, but the
installed `espn-api` library also supplies zero when the corresponding split or
field is absent. `player_season` stores actual results only. Neither those
defaults nor old report zeros establish that ESPN forecast zero points.

## Capture and storage

Weekly mode requests raw player cards for the union of draft and current-roster
player IDs in batches of 50. `features/provider_projections.py` accepts only
splits with the requested `seasonId`, `statSourceId = 1` (projection), and
`statSplitTypeId = 0` (full season). It reads `appliedTotal` and `appliedAverage`
independently. Omitted, null, nonnumeric, Boolean, and nonfinite values remain
missing. Explicit numeric zero is valid. An average alone never implies a total.
Conflicting duplicate projection splits are treated as unavailable.

The append-only `provider_projection_snapshots` table records league ID, player
ID, season, horizon, source, UTC observation time, and nullable total/average.
League scope matters because applied fantasy points depend on scoring rules.
There is no destructive update of a previous forecast when ESPN omits data.
Reports select the newest total-bearing snapshot for the league/season/player;
if no total exists, the newest average-only snapshot remains visible as partial
data. They do not combine fields across captures. Capture timestamps remain
visible so a retained older observation is distinguishable from a fresh fetch.
The table does not record a provider issue date or historical scoring-rule
version; comparisons assume the league's scoring rules are unchanged.

Shared object normalization uses the exact `<year>_projected` split rather than
bare default-valued attributes. Without raw evidence, a nonzero split value is
retained with `espn_api_season_split` provenance; a normalized zero is ambiguous
and stays unavailable. Where raw cards are already available (weekly capture and
waiver inputs), explicit zero is preserved. The existing predictive engine can
use a verified zero average, while legacy bare zero model inputs remain unknown.
These paths do not create a new forecasting model.
Attached forecast metadata must also carry a matching season, full-season horizon,
nonempty source and valid observation timestamp. Malformed metadata remains
unavailable and does not fall back to potentially conflicting wrapper values.

## Explicit capture for stored draft picks

Projected points are fields on append-only rows in
`provider_projection_snapshots`, created by the existing
`migrations/001_provider_projection_snapshots.sql`. They are not a mutable
column on `draft_picks`: a pick is fixed, while provider observations can change
and need their own league, season, source, horizon and UTC capture timestamp.
No additional schema migration is needed when migration 001 is already present.

The ordinary `--mode draft` board does not persist provider forecasts. To
explicitly capture them for a selected draft without running weekly ingestion:

```sh
venv/bin/python -B main.py --mode draft --capture-draft-projections --season 2026 --output-dir /path/to/new-capture-directory
```

`--season` means NBA season ending year: 2026 is 2025–26, not the calendar-2026
draft for season ending 2027. This capture route requires explicit `--season`
and `--output-dir` and rejects team/week, waiver and model-review options. It
exits before the general League constructors, modeled draft board, weekly
pipeline and email workflow. An existing output directory is rejected.

The route preflights the existing schema and compares the stored draft to
ESPN's exact league/season draft using overall pick, team, player, round and
round pick. This check is necessary because `draft_picks` itself is scoped by
season rather than league ID. A mismatch stops before inserting anything. The
route never changes picks, imports actual season results or repairs a mismatch
silently. Credentials stay in the existing environment and are not exported.

Raw player-card forecasts are fetched in the existing bounded batches, with
missing versus explicit zero preserved. Available observations append to the
existing table in one transaction; omissions do not overwrite prior snapshots.
The output contains every stored pick, including those with missing forecasts:

- `draft_pick_projections.csv` includes pick/player IDs, league and season,
  projected total/average, horizon, source and observation time. Missing values
  are blank. `projection_status` distinguishes available total, average-only
  and missing forecasts.
- `observation_selection` distinguishes `current_capture`, `retained_capture`
  and `unavailable`. Selection preserves the existing total-bearing preference:
  an older total/average bundle may be retained over a new average-only capture.
  Fields and timestamps from different observations are never combined.
- `capture_summary.json` records counts, transaction completion and historical
  limitations. Every report row is marked `not_verified_pre_draft`.

These are observations captured now. Even a provider forecast for an old season
cannot be treated as known before the draft merely because its season matches.
This export does not certify a complete pre-draft player pool, historical rules,
keeper state, draft cutoff or forecast issue time, and produces no draft-night
grade. The offline frozen replay continues to enforce its original requirements.

The route prepares files in a temporary sibling directory, commits the database
transaction, then publishes the new directory. Pre-commit failures roll back;
a failed commit acknowledgment is explicitly reported as uncertain. If publishing
the directory fails after commit, the error explicitly says storage committed.
Inspect storage before retrying an uncertain operation; rerunning a successful
capture intentionally appends a new observation rather than overwriting history.

## Report contract

- `season_year` identifies the actuals' season; `projection_season`,
  `projection_horizon`, `projection_source`, and `projection_observed_at` identify
  the forecast. `projected_avg_points` is retained even for partial snapshots.
- A finite actual total and forecast total, matching season, full-season horizon,
  source, and valid observation time are required to calculate differences.
- Missing or unverified forecasts keep blank differences, percentiles, and
  percentages. Status distinguishes `Missing Projection`, `Unverified Projection`,
  `Incompatible Projection`, and `Missing Stats` (no actual result).
- `projection_status` is `Above Projection`, `At Projection`, or
  `Below Projection`, based on the signed actual-minus-forecast difference.
  Relative cohort percentile no longer determines these labels. This avoids
  calling the least-negative result "above" or a large singleton miss "near".
- `percent_above_projection` divides the difference by the absolute forecast
  total. It stays blank for a verified zero forecast, although its absolute
  difference and cohort percentile are valid. An actual zero is a valid result.
- The draft/current-roster union remains visible, including players whose
  forecast or actual result is unavailable. Draft zero-filling does not fill
  missing projection columns.

## Rollout (explicit approval required for a live database)

1. Review `migrations/001_provider_projection_snapshots.sql` and verify the intended
   MySQL target. The migration adds one table and index; it does not alter
   `player_season`, update existing rows, or backfill old report values.
2. Obtain explicit authorization for applying that migration to the named live
   database, then apply it once using the normal database administration process.
   The script intentionally does not hide a conflicting existing table with
   `IF NOT EXISTS`.
3. A subsequent authorized weekly run captures whatever ESPN actually supplies.
   The schema preflight occurs before the pipeline's existing writes. If the
   table is missing or unreadable, weekly mode stops with an actionable message;
   it never creates the table itself. Ordinary decision-only modes do not need this table; explicit draft capture does.
4. Inspect new CSV coverage and source/capture timestamps. If ESPN supplies no
   forecasts and none have been stored, the honest result is missing projections.
   There is no automatic reconstruction of past forecasts.

No live migration, production capture, or report regeneration is part of the
unit tests. `tests/test_draft_projection_capture.py` uses synthetic ESPN cards
and disposable SQLite storage to cover exact draft identity, retention, zeros,
transaction failure paths, export safety and early CLI dispatch. Existing checked-in CSVs may still show old misleading zeros until
an authorized run regenerates them. Retain old outputs as historical artifacts,
not as forecast evidence. Disabling this code requires reverting the code change;
the additive snapshot table can remain intact for retention and review.

## Validation

`tests/test_provider_projections.py` exercises real installed `Player` default
behavior with synthetic responses, raw ingestion, mocked requests, production
INSERT/SELECT logic using a disposable SQLite fixture (with bind translation),
forecast retention, league/season isolation, missing/zero/partial values, draft
recovery, classification, and CSV round trips. `test_projection_analysis.py`
covers union behavior, ties, and relative percentiles. Full check:

```sh
venv/bin/python -B -m unittest discover -s tests
```

SQLite exercises the data contract; it does not validate MySQL-specific DDL,
permissions, or deployment. Review and test the migration on a disposable MySQL
database before live rollout. Unit tests make no live ESPN or MySQL requests.
