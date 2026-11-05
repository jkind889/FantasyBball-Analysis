# Working in FantasyBball-Analysis

## Start here

- Inspect the current branch, `git status`, applicable guidance, and nearby code
  before editing. Preserve unfinished user work and existing guidance. Local
  branches can be newer than GitHub or historical notes.
- Read the relevant guide under `docs/` and tests before changing a feature.
  Keep work within the requested scope; do not add forecasting models or alter
  league rules as an incidental fix.

## Repository map

- `main.py`: pipeline orchestration, ESPN reads, MySQL ingestion, CSV reports,
  and optional email. It executes on import; do not import it in ordinary tests.
- `cli.py`: argument parsing, validation and the opt-in wizard; no provider or
  storage I/O, so it is safe to test directly.
- `cache.py`: shared ESPN normalization and DB-first previous-season lookups.
- `database.py`: environment-based MySQL connection. Do not open it in tests.
- `features/`: retrospective reports, schedule/lineup analysis, predictive
  inputs and scoring, decision modes, waiver snapshots, historical demos, the
  streaming planner and its Claude review, ESPN injury news (`player_news.py`,
  `news_scan.py`),
  offline draft replay, and explicit draft forecast capture.
- `features/provider_projections.py`: provider forecast normalization and
  snapshot persistence; `migrations/`: reviewed, explicitly applied schema SQL.
- `tests/`: `unittest` regressions with synthetic providers and disposable data.
- `data/` and `reports/`: generated artifacts. Only `data/draft.csv` and
  `data/players.csv` are tracked; `reports/` is untracked. Do not regenerate them
  incidentally or treat an old report as evidence of current provider data.
  `data/news_history/` is git-ignored and can't be re-fetched: do not delete it,
  and do not write to it from tests.

## Data contracts

- Keep player ID, league, season ending year, and horizon explicit. A full-season
  provider forecast, current actual total, and modeled rest-of-season estimate
  are different quantities. Preserve source and UTC capture time; capture time
  does not establish when ESPN issued a forecast.
- Missing forecasts stay null/blank. `espn-api` convenience attributes default
  missing projections to zero; raw field presence is needed to verify a zero.
  Preserve genuine numeric zero. Never synthesize historical provider forecasts
  from actual results, a current model, or a different season.
- Retain valid stored forecast observations when a provider response is missing.
  Do not combine values and metadata from different observations. Comparisons
  require compatible season/horizon and provenance; unavailable rows remain
  visible without fabricated differences, ranks, or performance labels.
- Draft-only and final-roster-only players must remain visible in union reports.
  Preserve shared free-agent caching and existing draft eligibility semantics.
- Keep pure scoring/report functions separate from provider and storage I/O.
  Test provider omissions, failures, and malformed data with captured/synthetic
  fixtures rather than live calls.

## Safety and validation

- Use the repository virtual environment. Full check:
  `venv/bin/python -B -m unittest discover -s tests`.
  Focused example:
  `venv/bin/python -B -m unittest discover -s tests -p 'test_provider_projections.py'`.
- Never run `main.py` as a routine test: weekly mode writes MySQL and reports and
  may send email. Use mocks or fresh disposable fixtures and temporary output
  directories. Do not use the user's live database or league/account settings.
- Never print or commit `.env`, ESPN cookies, database credentials, or email app
  passwords. Do not log raw authenticated provider responses.
- Prepare schema changes as reviewable SQL and document rollout requirements.
  Applying to a live database, backfills, and other live data changes require
  explicit authorization for the target. Do not auto-migrate at startup.
- Run focused tests while developing, then the full suite for shared data-path
  changes. Report actual results and unverified integration environments. Keep
  documentation aligned with changed contracts. Do not push or deploy unless
  requested.
- Review every change before committing (correctness, edge cases, these data
  contracts) and fix what the review finds first. Commit after every fix or
  milestone rather than in one large commit, using `feat:`, `fix:`, `docs:` or
  `chore:` prefixes. Each commit must pass the full suite when checked out alone.
  Keep generated `data/`/`reports/` artifacts out of commits unless requested.
