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
credentials. Five live requests to `claude-opus-5-5` on one matchup's data all
validated, each using about 10,700 input and 900 or fewer output tokens. All five chose
the deterministic plan and cited no evidence. On 2026-11-10, three more `claude-opus-5-5`
reviews of other teams' live snapshots (replayed with their exported news) all validated
and cited ESPN blurbs. Two chose a different plan: HOLD instead of dropping a star whose
OUT status was a one-game rest, and a plan avoiding an add downgraded to out. Other
models are unverified.

Failed requests retain bounded `request_diagnostic` metadata in the fallback:
a stable error code, HTTP status when received, and an allowlisted stop reason
when relevant. Codes distinguish transport/HTTP failures, response size,
envelope JSON/shape, truncation, content shape and answer JSON; an answer that
parses but fails plan/evidence validation is `answer_failed_validation`, with the
rule it broke as fixed text in `validation_failure`. When a
response was received, the diagnostic keeps its HTTP status and allowlisted token
usage, and the explanation text prints them. A content-shape failure also lists
allowlisted block type names, never block contents. Response bodies,
headers, provider error messages and credentials are never included. The adapter
ignores `thinking` and `redacted_thinking` blocks without keeping them, because
some models cannot turn thinking off, and requires exactly one text block as the
answer. It does not retry or relax validation. A transport or response-validation failure does not prove that generation or
billing did not occur; inspect service-side usage before approving a retry.

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
- Current injury availability is held constant in numerical forecasts, with one
  exception: an OUT player whose ESPN `injuryDetails` type is `Rest` counts from
  ESPN's expected return date, at his normal rate. Injured OUT players stay out all
  week because for injuries ESPN's date is usually just the next game; using those
  waits for sources beyond ESPN. A return date on or before today while the player
  is still OUT is stale and ignored, as are out-for-season players, suspensions and
  IR slots. Snapshots keep `injury_return_date`, `injury_type` and `out_for_season`;
  older snapshots without them replay as before, and Claude sees the type and date.
  Context helps compare plans but does not introduce a new injury or opponent
  forecasting model. Protect core players explicitly; season VORP is comparison evidence,
  not an automatic protection rule.

## Dated context input

No opponent-history feed is added. The existing snapshot provides opponents, game
dates, and injury status. ESPN injury news is available with
[`--streaming-news`](#espn-injury-news). Other evidence uses this explicit interface
(synthetic example; IDs must exist in the supplied snapshot):

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
not established causal matchup advantages. Sources and content are supplied and not
independently verified. Missing/stale evidence is exported explicitly.
Files are limited to 64 KiB / 100 entries; sources to 500 characters and summaries to 1,000.
Entries may also carry `retrieved_at` (timezone-aware) and `source_item_id` (at most
64 characters) to keep a provider's own item ID and the retrieval time.

### ESPN injury news

`--streaming-news` (live runs only) adds recent injury blurbs to this context using
`features/player_news.py`. It is off by default and is also offered in the menu.

- **Requests:** one per player, without cookies, bounded by timeout and response size.
  Players in candidate moves come first, then the roster, up to 40 players. A failed
  request or feed is reported and never blocks planning.
- **Filtering:** only `Rotowire` items tagged to that player are kept. Clips (`Media`,
  or other types with `video`) and other items such as `Story` are dropped and counted.
- **Injury routing:** a heuristic on the headline only. The marker must sit on the
  player's own name, such as `Smith (ankle)` or `Smith underwent`, because story text
  and later clauses often describe teammates. Blurbs can still be missed or misrouted.
  Clear wording (`ruled out`, `set to miss`, `minutes restriction`) is routed as is.
  Wording that may not mean an injury (`did not practice`, `didn't participate`,
  `limited`, `will rest`) still qualifies, but keeps more of the story so the review
  can judge it. A clear marker on the name wins over judgment wording, except
  `available`: a returning player may not be at full strength, so availability always
  keeps the story, even under an injury tag. Negated availability (`won't be
  available`) is not a return. Tags that name no injury (`rest`, `coach's decision`,
  `not injury related`, `recently traded`, `two-way`, `personal`, `trade pending`,
  `recently signed`) don't count alone; wording after them still does, as in
  `Smith (rest) has been ruled out`.
- **Time window:** published within the 24 hours before the decision snapshot, matching
  the context freshness rule. Newer items are excluded rather than used in hindsight.
  `observed_at` is ESPN's declared publish time; `retrieved_at` is when it was fetched.
- **Budgets:** at most two blurbs per player, 240-character summaries (1,000 for
  judgment wording, which held 99% of saved blurbs whole) and about 4 KB in total.
  Caller-supplied entries always win. Everything trimmed is counted.
- **History:** each live news run adds to `data/news_history/blurbs.jsonl`, one JSON
  object per line. It is git-ignored and exists only on this machine. It keeps every
  blurb that passed the filtering above, whatever its age or topic and with its full
  story, because ESPN's feed is capped and old items can't be fetched again. Rows are
  never rewritten: `retrieved_at` is when that text was first seen, and an edited blurb
  gets a new row. `retrieved_at` shows when we had a blurb, not that ESPN's `published`
  time is right. Nothing reads this file yet, so it has no effect on plans or reviews.
  A failed save is reported and never blocks planning.
- **League-wide scan:** `venv/bin/python -B main.py --mode news-scan` adds blurbs for
  every player ESPN currently flags (any status other than `ACTIVE`) to the same history.
  It reads rosters and the free-agent pool, then requests one player at a time with a
  short pause, most-owned first. It is limited to 250 players and 5 minutes, and stops
  after 5 failures in a row. It exits with status 1 if the scan was partial. It doesn't
  touch MySQL, reports, email, plans or Claude. It isn't scheduled, so run it about
  weekly: a player hurt and back between scans is missed, and players with no status
  are counted but not scanned.
- **Review input bound:** a real six-day week measured about 14.5 KB without news and
  19.3 KB with four blurbs, because each player with fresh evidence adds game rows to
  every plan. The bound is 32 KB. When the payload is too large, the review withholds news
  from the lowest priority upward and reports the count as `evidence_trimmed`. Caller
  evidence is never withheld, so a payload that is still too large skips the review as
  `input_bound_exceeded`.

News never changes projected points, rankings or deterministic plan selection. It only
gives Claude dated evidence to cite. Without `--claude-review` it is exported but has no
effect on selection. Important limits:

- No blurb does not mean a player is healthy; the feed's coverage stays unknown.
- A long absence last reported more than 24 hours ago is not news here. It relies on
  the snapshot's injury status.
- The endpoint is undocumented and can change. Its terms for storing blurbs or sending
  them to a model were not reviewed; blurb text goes to Anthropic only with
  `--claude-review`.

Exports add `streaming_context.json` (always within the 64 KiB load limit, in compact
form if needed) and a `news` section in `streaming_plan.json` with
per-player counts. Replays stay offline, so `--streaming-news` is rejected with
`--snapshot`. Pass the exported context with `--streaming-context` to reproduce a run.

## Claude boundaries and fallback

The adapter sends at most eight available plans, retaining the best plan per
acquisition count including HOLD, relevant players' usable game dates/opponents,
season values, fresh evidence,
missing-context coverage, and limitations. It uses the
[Anthropic Messages API](https://platform.claude.com/docs/en/api/overview).
One request is allowed per invocation, with no retries or redirects, a 5-second
connect / 120-second read timeout, 32,000 UTF-8 input bytes including instructions,
4,000 output tokens, and a 64,000-byte response limit. Thinking counts toward the
output tokens. The read timeout is long because a non-streaming response sends nothing
until generation finishes. The byte cap is a conservative input-token bound, not a
monetary cost estimate. Oversized inputs skip the model.

The model returns only a selected plan ID and structured comparison reasons with
player/evidence IDs. The request sends that shape as a JSON schema through
`output_config.format`, so a model without structured outputs fails with an HTTP error
and keeps the deterministic plan. The schema fixes the shape only; Python still checks
every ID, count and rule, and the prompt states those rules. Unknown IDs, extra numeric fields, unsupported reasons,
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
When every plan is partial, the text report labels HOLD as a **fallback**, not a
recommendation or the best scored plan. Positive gains on partial plans remain
provisional. A shared blocker legend names affected players, suggests checks,
and lists the blocker codes for each partial plan. Up to 20 distinct reasons
are printed; the CSV `issues` column and JSON retain every reason.

Claude statuses distinguish skips before the API request from validated output.
`insufficient_plan_data` means there were no evidence-complete plans to compare.
`error_or_invalid_output` does not establish whether a request was dispatched;
that status also includes local validation failures. Optional injury/matchup
context coverage is summarized separately for roster/plan players and the full
pool; missing optional context is not a plan-completeness gate.

Terminal allowance values say `unlimited` where the JSON stores `null` and the
CSV stores a blank. Daily acquisition limits still apply and are shown separately.
Numeric totals, export schemas, evidence gates, and selection policies are unchanged.
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
