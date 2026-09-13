# Live decision support: product and implementation scope

Status: section 1 is implemented as a CLI advisor with recent form, daily lineup
comparison, snapshot replay, and calendar CSV exports. Interactive UI, live draft
assistance, and later increments remain proposed. See the
[waiver advisor guide](waiver_advisor.md) for supported rules and limitations.  
Created: September 10, 2026. Updated: September 13, 2026.

## Objective

Make the app useful at the moment a manager makes a decision: which player to
add, who to drop, which remaining games can improve the lineup, and who to draft
at the current pick. Every recommendation should show the expected benefit,
the alternatives, the assumptions, and when its inputs were refreshed.

This scope covers four connected capabilities:

1. A roster-aware waiver add/drop advisor.
2. A usable-game and legal-lineup engine.
3. A draft assistant that updates after each pick.
4. A remaining-week calendar for inspecting and comparing roster plans.

The first release targets one ESPN head-to-head **points** league. Daily lineup
changes are the initial supported format. League settings must be verified;
weekly-lock leagues require a separate period-level calculation and must not
silently receive daily-lineup recommendations.

## Existing foundation and gaps

The current implementation is a Python reporting pipeline with CSV outputs.
See [the predictive engine](predictive_engine.md) for the existing design.

| Existing component | Reuse | Gap to close |
| --- | --- | --- |
| `features/projection_inputs.py`, `features/projections.py` | Prior/current/ESPN blended points-per-game estimates | Retain data-quality information and injury context; separate player availability from team games |
| `features/nba_schedule.py` | Team schedule and fantasy-period mapping | Preserve game identity, tipoff timestamps, status, and local dates; exclude elapsed opportunities |
| `features/player_value.py` | Rest-of-season value over replacement | Use league settings and actual eligible slots; treat current positional replacement estimates as approximate |
| `features/decision_modes.py` | Draft, waiver, and start/sit entry points | Waivers are not roster-aware; draft rankings do not track picks; start/sit optimizes a single full-week lineup |
| Existing lineup assignment approach | Legal player-to-slot assignment | Apply it per day with fixed locked assignments, bench options, and transaction timing |

Today, the waiver board counts every game in the selected fantasy week, even
when run midweek. The schedule normalizer converts timestamps to UTC dates and
discards tipoff time. Starting slots come from a default template. These are
foundational limitations to address before presenting recommendations as live.

## Shared rules and data contract

All four capabilities should consume the same versioned decision snapshot.
Calculations should be pure functions over this snapshot, allowing offline
tests and replay without contacting ESPN.

| Input | Required information |
| --- | --- |
| Decision context | League, season, team, timezone, `as_of` timestamp, target scoring-period boundaries, snapshot ID |
| League rules | Scoring format, starting/bench/IR slots, eligibility, roster limits, lock policy, acquisition limits and usage, any games/starts caps |
| Players | Stable ID, name, NBA team, actual eligible slots, ownership, injury status, projection and source coverage |
| Recent game logs | Player/game IDs, completion time, fantasy points under this league's scoring, played/DNP status, minutes when available, source and retrieval time |
| Schedule | Stable game identity where available, UTC tipoff, local display date, opponent, status, scoring period |
| Current lineup | Assigned slots, locked players, and roster membership effective at the decision time |
| Transactions | Free-agent versus waiver status, earliest usable acquisition time, undroppable players, relevant roster restrictions |
| Draft session | Draft format/order, picks, keeper assignments if applicable, available pool, user team, next pick |

Discover supported ESPN fields during implementation. Missing rules may be
supplied through explicit local configuration, with their source shown in the
app. Do not infer waiver eligibility from the presence of a player in a generic
free-agent response. Confirm coverage of the available player pool.

Use league scoring-period boundaries rather than assuming every matchup ends
Sunday. Store time in UTC and display it in the configured league timezone.
Preserve existing CSV consumers when extending schedule fields.

Every result must include `snapshot_id`, `as_of`, `generated_at`, target horizon,
and relevant limitations. Missing schedule or projection data is **unknown**,
not zero. Incomplete data may support an explicitly partial comparison, but
must not produce an unconditional best-move recommendation.

## 1. Roster-aware waiver add/drop advisor

### Manager experience

Select a team and time horizon, optionally protect players from being dropped,
then receive ranked legal moves. Selecting a move opens its calendar preview
and compares the resulting lineup with holding the current roster.

The main recommendation should read like this illustrative example:

> Add Player A and drop Player B: +28 projected lineup points through the end
> of this matchup. A contributes two starts; B would contribute one. This move
> uses one acquisition and becomes effective tomorrow.

Include a permanent **Hold current roster** option with a gain of zero. An empty
roster spot should produce an add-only option, without forcing a drop.

### First-release behavior

- Evaluate each eligible available player against each legal, unprotected drop
  candidate, plus any legal add-only action.
- Apply the transaction at its effective time. Preserve earlier roster state
  and locked assignments according to the league's rules.
- Re-optimize the entire lineup over the remaining matchup for each candidate.
  Account for other rostered players changing slots or moving to the bench.
- Reject illegal moves before ranking: undroppable or protected players,
  acquisition limits, roster/position limits, lock restrictions, and invalid
  injury-reserve configurations.
- Show waiver-dependent moves separately from immediately available adds.
  Treat an unawarded claim as conditional; never assume the user will win it.
- Rank by projected gain over holding the roster. Break ties consistently,
  favoring fewer acquisitions and then stable player IDs.
- Display season-long value alongside short-term gain so a small weekly
  improvement does not conceal the cost of dropping a valuable player.
- Incorporate the last 5–6 completed appearances into short-term streaming
  projections for both available and rostered players. Sustained recent production
  should carry more weight than the season baseline for this horizon.

### Recent form and streaming value

For short-term pickups, prefer a player producing well now over a comparable
player whose season average conceals a recent decline. The default recent window
is the last **six played games**, with a five-game option. These are fantasy
points using the league's scoring rules, not NBA points scored. A 30-point
threshold is useful as a configurable display filter, not a universal cutoff.

Show season average, last-five average, last-six average, recent median, sample
size, date range, and the change from season average. Include how many recent
games reached the chosen threshold, so a consistent run of 30+ games is visibly
different from one exceptional performance. Show recent minutes when available
as supporting context; automated role-change analysis remains a later feature.

Initial, configurable scoring proposal for the remaining-matchup horizon:

```text
baseline_ppg = existing prior/current/ESPN blended projection
recent_form_ppg = 0.50 * recent_mean + 0.50 * recent_median
recent_weight = 0.60 * min(eligible_recent_games / selected_window, 1)
streaming_ppg = recent_weight * recent_form_ppg
              + (1 - recent_weight) * baseline_ppg
```

With a complete window, recent form receives 60% of the weight. Combining mean
and median reduces the influence of a single extreme result while allowing a
consistent hot streak to raise the estimate. Cold streaks lower it symmetrically.
These weights are a starting product rule to validate against historical
snapshots, not established predictive accuracy. The baseline already includes
some recent games; this deliberately emphasizes recency rather than treating
the inputs as independent evidence.

Illustrative comparison with equal schedules, availability, and lineup fit:

| Player | Season average | Baseline projection | Last-six mean / median | Streaming estimate |
| --- | --- | --- | --- | --- |
| A: sustained hot streak | 24 | 25 | 34 / 34 | 30.4 |
| B: recent decline | 30 | 30 | 20 / 20 | 24.0 |

Player A should hold more short-term value in this example despite the lower
season average. Over three usable starts, the estimates are 91.2 versus 72.0
points. Actual add/drop gain still requires re-optimizing the full roster;
these isolated totals are not a substitute for that comparison.

Apply the same streaming projection to every player in the hold scenario and
every proposed-move scenario. Feed it into the daily lineup engine and calendar.
Do not add a second hot-streak bonus after computing lineup gain. A hot player
can still rank below another option if they have fewer usable games or cannot
be acquired in time. Keep the existing rest-of-season projection separate; the
short-term recency weight does not automatically change draft or season rankings.

### Recent-data eligibility and fallback

- Use only completed appearances and information available at `as_of`; exclude
  in-progress games, duplicate game records, and later results from replays.
- Retrieve the last six appearances for display, then use the selected five- or
  six-game window for scoring. Exclude appearances older than a configurable
  freshness limit, initially 21 days. Show when the scored sample differs from
  the historical last-five/six display because of this limit.
- A DNP is not a zero-point appearance. A game actually played for zero fantasy
  points is included. Availability and injury restrictions are handled separately.
- Fewer than three eligible appearances: use the baseline only and label recent
  evidence insufficient. Three to five appearances in a six-game window receive
  proportionally less recent weight, with the actual count visible.
- Missing, incomplete, or stale logs must not be interpreted as a cold streak.
  Fall back to the existing baseline and label the result as baseline-only.
  Surface unequal recent-data coverage between compared players and treat those
  comparisons as partial rather than definitive streaming recommendations.
- If no baseline projection exists, use the app's missing-projection handling;
  do not invent a baseline or silently present a recent-only estimate as equivalent.
- Verify a per-game source for rostered players and waiver candidates during
  implementation. Weekly totals alone cannot establish a five- or six-game run.

The recommendation explanation should name the contribution of recent form,
for example: “34 fantasy points per game over six recent games, versus 24 for
the season; streaming estimate 30.4, with two usable starts remaining.” Include
injury assumptions and whether the estimate fell back to the baseline.

For horizon H:

```text
baseline = maximum projected legal lineup points with current roster over H
candidate = maximum projected legal lineup points after proposed move over H
net_gain = candidate - baseline
```

Already-earned points are identical across alternatives and excluded from this
future-gain comparison. Do not use the incoming player's total minus the outgoing
player's total as a substitute for re-optimizing the roster.

### Result fields

| Field group | Fields |
| --- | --- |
| Action | Add/drop IDs and names, action type, availability, effective time, conditional status |
| Impact | Baseline points, candidate points, net gain, baseline/candidate usable starts, acquisitions consumed |
| Tradeoff | Incoming/outgoing rest-of-season value, projection coverage, injury assumptions |
| Recent form | Season average, last-five/six means, recent median, selected/scored window and dates, count above threshold, baseline and streaming estimates, applied weight, fallback reason |
| Explanation | Which dates and slots change, rejected-action reasons, horizon and freshness |

### Later increments

- Next matchup and a clearly bounded two-matchup horizon, evaluated with the
  same lineup engine.
- A separate rest-of-season view. Until long-horizon availability is modeled,
  label value-over-replacement comparisons as estimates, not exact usable points.
- Plans using multiple acquisitions in sequence, with effective dates and a
  shared acquisition budget. The first release evaluates one move at a time.
- FAAB bid advice only after budget, bidding rules, and useful history exist.

### Acceptance criteria

1. A lower-average player can rank first when they improve the actual lineup more.
2. A player whose games all overlap a stronger full lineup adds no usable value.
3. A waiver clearing after a game cannot contribute that game's points.
4. Protected and undroppable players never appear as proposed drops.
5. No legal positive-gain move produces a clear hold recommendation.
6. Empty rosters, full rosters, multi-position players, exhausted acquisition
   limits, and unavailable projections are handled explicitly.
7. Every displayed gain can be reconstructed from its calendar comparison.
8. With equal usable games and lineup fit, the sustained-hot-streak player in
   the example above outranks the season-long 30-point player in a recent slump.
9. One extreme game influences the estimate less than using the recent mean
   alone; consistent high or low production changes the estimate in its direction.
10. Five/six-game selection, small samples, stale logs, DNPs, played zero-point
    games, duplicates, and future results follow the stated eligibility rules.
11. Hold and proposed-move calculations use identical recent-form rules, and
    the calendar reconciles to the resulting streaming-adjusted points.
12. Draft and rest-of-season projections remain unchanged by this short-term
    adjustment; any future horizon-specific weights require separate validation.

## 2. Usable-game and legal-lineup engine

### Definition

A scheduled game is a **usable start** when the player is on the effective
roster, eligible and available under the stated assumptions, and assigned to a
legal starting slot for that scoring day. Games already completed or locked
out of a proposed action cannot become new usable starts.

The optimizer maximizes projected points, not the number of games. Two lineups
may have the same number of starts and different values.

### First-release calculation

1. Resolve the remaining decision window using `as_of` and the league's scoring
   periods. Preserve locked lineup decisions and distinguish in-progress games
   from future choices.
2. Generate player-day opportunities from the schedule and effective roster.
3. Determine availability, slot eligibility, and whether a lineup change is
   still legal. Use actual ESPN eligible slots, normalized consistently.
4. Solve player-to-slot assignment for each day, allowing empty slots and bench
   assignments. A player occupies at most one slot per day; slots respect their
   configured capacities.
5. Aggregate projected points, usable starts, and reasons for unused games.

Independent daily assignment is appropriate only when no rule couples decisions
across days. A matchup-wide games/starts cap requires a horizon-level constrained
optimizer. Detect such rules and either support them before enabling advice or
mark the league configuration unsupported.

For remaining-matchup streaming decisions, use the recent-form-adjusted points
per game defined in section 1, falling back to the existing projection when the
recent-data rules require it. The advisor, hold comparison, start/sit view, and
calendar must share the same projection basis for a given decision snapshot.
Do not add invented opponent adjustments or injury probabilities. An OUT
designation should retain its timestamp and meaning; it is not proof that the
player will miss the entire rest of the season. For uncertain future availability,
show explicit scenarios or a limitation rather than false precision.

### Explainability and edge cases

Each player-day record should carry one of: recommended start, bench due to
stronger options, no game, unavailable, locked, pending acquisition, or unknown.
Include projected contribution and assigned slot where applicable.

Handle postponed/canceled games, unknown tipoffs, timezone/date boundaries,
multi-position eligibility, and official changes to scoring-period assignment.
Do not declare a game actionable if its lock time cannot be established.

### Acceptance criteria

1. Completed games never inflate remaining-week projections.
2. A game starting tonight is handled differently before and after its lock.
3. Two competing players cannot occupy the same slot; multi-position players
   can move to create the best legal lineup.
4. Locked players retain their legal assignments during what-if comparisons.
5. An idle starting slot remains visible when no eligible player can fill it.
6. The same snapshot produces identical results across waivers, start/sit,
   and the calendar.
7. Unsupported locks or caps are surfaced before recommendations are shown.

## 3. Live draft assistant

### Manager experience

Create or resume a draft session with the league's roster settings, draft order,
and user team. Mark each selected player as drafted and immediately update the
available board, roster summary, positional depth, and recommended next picks.

The initial supported format is a snake draft. Keeper assignments can be loaded
explicitly if they are reflected correctly in both roster and pick state.
Auction drafts and traded-pick logic need separate support before being enabled.

### Three recommendation views

| View | Question | Initial method |
| --- | --- | --- |
| Best available | Who has the strongest standalone projected value? | Available players ranked by season projection/value over replacement |
| Best fit | Who improves my developing roster most? | Marginal projected roster value with legal slot assignment and a consistent replacement-filled completion baseline |
| Best pick now | Where is the cost of waiting greatest? | Value tiers, remaining positional depth, picks until next turn, and transparent availability scenarios |

Best fit should not simply prioritize any empty position or declare every bench
pick worthless. Model a completed roster using consistent, unique replacement
players from the remaining pool for unfilled slots; disclose this assumption.
Account for bench depth as a separate stated factor. Revisit the current
approximate positional replacement logic before using it as the only fit score.

Without a validated model of opponent picks, show scarcity and scenario-based
reasoning for “best pick now.” Do not present a numeric probability that a player
survives until the next turn. A later version can use sourced ADP and calibrated
pick simulations; ESPN projected points are not ADP.

### First-release behavior

- Search/filter the board by player, position, tier, and availability.
- Mark a player drafted to a team; advance the correct pick in snake order.
- Undo or correct picks and rebuild downstream recommendations.
- Show the user's roster, remaining needs, current pick, next pick, and picks
  before that next turn.
- Show several alternatives with projected value, fit, tier, and explanation.
- Persist session state locally so refreshes or restarts do not lose picks.
- Label projection season and coverage; handle rookies or missing priors
  explicitly. Preseason estimates must use the intended upcoming season.
- Keep recommendation computation local between data refreshes so each manual
  pick does not trigger a new full ESPN fetch.

Manual pick tracking is sufficient for launch. Automatic ESPN draft updates are
a later integration requiring verification of in-draft availability, latency,
and completeness. Reconcile imported picks by stable IDs, surface conflicts, and
retain manual correction. Do not assume post-draft data proves live sync works.

### Acceptance criteria

1. A selected player immediately disappears from available recommendations.
2. Picks and next-turn calculations work at both ends of a snake round.
3. Undo restores the board and roster to the corresponding prior state.
4. Reloading a saved session preserves picks, settings, and protected state.
5. Best available and best fit can differ, with an intelligible explanation.
6. Missing ADP produces no fabricated availability percentages.
7. Duplicate picks and unknown player IDs are rejected without corrupting state.
8. A completed or paused draft can still be inspected and exported.

## 4. Remaining-week calendar

### Purpose and layout

Make the remaining matchup understandable at a glance and provide the visual
explanation for waiver and lineup recommendations. This is a core part of the
first waiver release, using the same calculation results as the advisor.

The header shows team, matchup period, timezone, decision time, last successful
refresh, and acquisitions remaining. Columns are scoring dates; rows are rostered
players, with starting-slot and per-day totals available alongside them.

Each game cell shows opponent, local tipoff, injury/availability state, projected
points, and recommended slot or reason the game will not count. Use text/icons
as well as color. Past days may be collapsed; completed and locked games should
remain distinguishable from upcoming opportunities.

Player details show season and recent averages, the selected five/six-game
window, and the streaming estimate used by the calendar. Label baseline-only
fallbacks. Changing the recent window recomputes both waiver rankings and
calendar totals together.

Illustrative compact view; numbers are examples, not live recommendations:

| Player / summary | Thu | Fri | Sat | Sun |
| --- | --- | --- | --- | --- |
| Player A | Start: SG, 24 pts | No game | Bench: full lineup | Start: UT, 24 pts |
| Player B | No game | Start: C, 30 pts | No game | No game |
| Usable starts | 1 | 1 | 0 | 1 |
| Projected points | 24 | 30 | 0 | 24 |

On narrow screens, show an individual day with previous/next navigation and a
persistent matchup summary. Empty slots should name their eligible positions.

### What-if interaction

- Toggle between **Current roster** and a selected **Proposed move**.
- Highlight added/removed players and every changed lineup assignment.
- Show daily and overall differences in usable starts and projected points.
- Explain when a proposed acquisition becomes effective and which games it misses.
- Allow the user to exclude a player from a scenario or protect them from a drop,
  then recompute all views against the same snapshot.
- Show a clear scenario label; a preview must not appear to be an executed ESPN
  transaction. Refreshing inputs should invalidate and recompute stale previews.

Opponent roster overlays and live win probabilities are later enhancements.
The first calendar focuses on the user's roster and a single proposed move.

### Acceptance criteria

1. Calendar contributions sum exactly to the advisor totals, subject only to
   documented display rounding.
2. Every unused scheduled game has a visible reason.
3. Dates and tipoffs render in the configured timezone, including games whose
   UTC date differs from the local scoring date.
4. Switching scenarios updates cells and totals together.
5. Missing or stale inputs are visible without being mistaken for an off day.
6. The full decision flow is usable on desktop and a narrow mobile viewport.

## Proposed implementation boundaries

These are proposed modules and outputs, not existing commands or delivered files.
Follow the existing pure-function feature pattern and keep network access at
the orchestration boundary.

| Area | Proposed responsibility |
| --- | --- |
| `features/league_rules.py` | Normalize and validate rules, eligible slots, lock policies, and configured overrides |
| `features/decision_snapshot.py` | Assemble one timestamped, internally consistent input snapshot |
| `features/nba_schedule.py` | Extend schedule normalization with identity, timestamps, and status |
| `features/usable_games.py` | Produce legal daily assignments and player-day explanations |
| `features/recent_form.py` | Normalize per-game evidence, calculate five/six-game form and short-term projections, expose coverage and fallback reasons |
| `features/waiver_advisor.py` | Generate legal actions, compare against hold, rank and explain results |
| `features/draft_assistant.py` | Maintain/replay draft state and compute recommendation views |
| Local dashboard layer | Render advisor, draft board, and calendar; keep business rules in feature modules |

Suggested exports: `waiver_recommendations.csv`, `remaining_week_calendar.csv`,
and a versioned `draft_session.json`. Persist stable IDs in every output.
Keep credentials out of exports and draft-session state. A local dashboard is
enough for the initial release; framework selection and cloud deployment are
separate implementation decisions.

Extend the current CLI so team selection applies to roster-aware waivers and
calendar output, with an explicit `as_of` option for reproducible analysis.
Preserve existing basic report behavior or document a deliberate migration.
Decision modes should remain independent of retrospective database writes, and
missing previous-season data should degrade projections gracefully.

## Delivery sequence

| Phase | Deliverable | Exit condition |
| --- | --- | --- |
| A: Rules and snapshot | Verified league settings, timestamp-preserving schedule, per-game log source, freshness/coverage metadata | Supported rules and missing inputs are explicit; reproducible offline snapshot works |
| B: Usable games and calendar | Daily lineup engine, remaining-week totals, current-roster calendar | Time, eligibility, lock, and calendar reconciliation cases pass |
| C: Waiver advisor | Recent-form streaming estimates, single add/drop or add-only ranking, hold option, calendar comparison | Hot/cold-streak and data-fallback cases pass; each recommended move is legal and its benefit is auditable by day |
| D: Draft assistant | Available board, three recommendation views, manual picks, undo, session persistence | A complete sample snake draft can be entered, corrected, resumed, and exported |
| E: Extensions | Longer horizons, multi-move streaming, verified draft sync, calibrated availability models | Each extension validated separately against its simpler baseline |

Draft session work can proceed after Phase A without waiting for the waiver UI.
Phases A–C are the first in-season release; Phase D is the first draft release.

## Validation and success measures

- Use deterministic synthetic fixtures for timing boundaries, unavailable
  players, positional congestion, acquisition delays, and illegal transactions.
- Validate optimizer results against exhaustive enumeration on small rosters,
  including cases where greedy player ranking fails.
- Replay saved snapshots through CLI and UI paths and verify identical totals.
- Evaluate the recent-form blend against baseline-only projections on later,
  unseen games using time-ordered snapshots. Check both projection error and
  realized lineup benefit; tune weights on earlier periods and evaluate on held-out
  periods. Include sustained streaks, outliers, and players returning from absences.
- Test a complete workflow: refresh, inspect current calendar, preview a move,
  protect a drop candidate, rerank, and export the result.
- Test draft entry, correction, persistence, and recommendation updates with
  a sample snake draft and a full available player pool.
- Target local scenario/pick updates within two seconds after data is loaded;
  measure on a representative league and report network refresh time separately.
- Record recommendations and assumptions as of their creation. Later measure
  realized lineup benefit versus holding and versus a highest-average-player
  baseline, without substituting future injury knowledge into the original inputs.

Release success means a manager can identify a legal move, explain its expected
benefit, inspect exactly when it helps, and act in ESPN without reconstructing
the analysis from separate CSVs. Predicted benefit is an estimate, not a promise
of realized performance.

## Dependencies and deferred scope

Implementation must resolve the actual league's lock behavior, acquisition
rules, games caps, draft format, and API field availability. Support explicit
configuration where practical; disable dependent advice when a required rule
cannot be established. Keep stale last-good data inspectable with its timestamp.

This scope does not include automated ESPN transactions, category scoring,
trade evaluation, a replacement for the season-long projection model, automated injury-news ingestion,
push alerts, paid data procurement, or a public hosted service. They can build on
the same snapshots later. The short-term recent-form adjustment in section 1 is
included in the first waiver release. Reliable numeric injury probabilities, draft survival
probabilities, and matchup win probabilities require separate modeling and
validation before appearing in recommendations.
