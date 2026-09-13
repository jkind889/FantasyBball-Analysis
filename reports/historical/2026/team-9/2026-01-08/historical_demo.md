# Historical waiver demo: ybgcorp

Season **2026**, matchup **12**, decision date **2026-01-08**.

This is a reconstructed demonstration using real games. Availability, injury state, and transaction eligibility are assumed, so these are simulated moves rather than verified historical waiver opportunities.

The input contains 15 rostered players and 358 inferred candidates. Baseline averages and recent form use only games before the decision date. This is a short-term gain comparison; it does not price the season-long cost of dropping a star. Use `--protect PLAYER_ID` to keep core players out of drop scenarios.

| Add | Drop | Season-to-date avg | Recent avg | Streaming estimate | Projected lineup gain | Actual gain, fixed planned lineup |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Kawhi Leonard | LeBron James | 50.4 | 63.5 | 56.2 | 65.7 | 50.0 |
| Jaime Jaquez Jr. | LeBron James | 30.9 | 34.7 | 33.7 | 54.5 | Unknown |
| Nikola Jovic | LeBron James | 19.0 | 32.3 | 27.5 | 35.9 | Unknown |
| Walker Kessler | LeBron James | 40.6 | Unknown | 40.6 | 34.6 | -60.0 |
| Pelle Larsson | Quentin Grimes | 19.8 | 27.0 | 25.9 | 16.9 | Unknown |
| Draymond Green | LeBron James | 26.0 | 32.5 | 30.6 | 14.7 | Unknown |

The table is ordered by projected gain, not the outcomes. Actuals score the lineups selected before looking at the remaining games; missing actuals stay unknown. An incremental gain can still be known when missing games are identical in both lineups and cancel. A single example does not establish prediction accuracy.

## Inspect the results

- [All add/drop comparisons](waiver_recommendations.csv)
- [Daily lineup contributions](remaining_week_calendar.csv)
- [Recent form for every player](recent_form.csv)
- [Saved replay snapshot](waiver_snapshot.json)
- [Held-out outcomes for planned lineups](historical_outcomes.csv)

## Reconstruction assumptions

- Reconstructed historical demo, not a contemporaneously saved snapshot or validated backtest
- Roster/ownership use the prior scoring day's archived rosters; same-day transactions may differ
- Candidates are inferred unrostered players; immediate availability and droppability are assumed
- One acquisition is assumed available; historical usage and pending claims are not reconstructed
- All non-IR players are assumed playable; historical injury bulletins are not reconstructed
- Eligibility and league rules may reflect later updates; NBA team is inferred from prior appearances
- Schedule uses actual historical dates and may include rescheduling unknown at the cutoff
- Baseline is pre-cutoff season-to-date mean; final-season totals and ESPN projections are excluded
