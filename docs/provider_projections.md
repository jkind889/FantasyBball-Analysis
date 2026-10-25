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
   it never creates the table itself. Decision-only modes do not need this table.
4. Inspect new CSV coverage and source/capture timestamps. If ESPN supplies no
   forecasts and none have been stored, the honest result is missing projections.
   There is no automatic reconstruction of past forecasts.

No live migration, production capture, or report regeneration is part of the
unit tests. Existing checked-in CSVs may still show old misleading zeros until
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
