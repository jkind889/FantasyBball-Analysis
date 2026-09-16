"""Ranking / VORP validation: did the engine's board order players the way
the season actually played out?

Given a projection-inputs frame for a *finished* season (see
:mod:`features.projection_inputs`), this replays the engine from a decision
point with only the information available then, scores realized value with
the same replacement-level math (:mod:`features.player_value`), and reports
how well each candidate ranking recovered the realized ordering — alongside
the naive baselines the engine is built from (ESPN projection, prior-year
pace) and the league's own draft.

Pure DataFrame code, no network access. See ``docs/predictive_engine.md``.
"""

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

import features.player_value as player_value
import features.projections as projections

DEFAULT_KS = (20, 50, 100)
# Adjacent-pair window for the pairwise win rate: how often the higher-ranked
# of two players ranked within this many spots of each other outproduced.
PAIRWISE_WINDOW = 10
PAIRWISE_TOP = 100

SUMMARY_COLUMNS = [
    "candidate", "target", "n_scored", "n_universe", "spearman",
    "n_common", "spearman_common", "pairwise_win_rate",
]


def preseason_inputs(inputs_df):
    """Strip current-season columns so the engine sees only what it would have
    known before opening night (prior-year pace + ESPN projection)."""
    df = inputs_df.copy()
    df["current_avg_points"] = np.nan
    df["current_games_played"] = 0
    return df


def realized_value(inputs_df, league_size, roster_slots=None, min_games=0):
    """Realized VORP: feed actual per-game production and actual games played
    through :func:`features.player_value.compute_player_value`.

    A player who never played (injured all year, or never got minutes) stays in
    the universe with zero realized value — a board that ranked them high
    should pay for it. ``min_games`` > 0 drops them, which is what the
    per-game target wants.

    Returns ``player_id, player_name, position, realized_pts_per_game,
    games_played, realized_vorp_per_game, realized_vorp_ros``.
    """
    df = inputs_df.copy()
    gp = pd.to_numeric(df["current_games_played"], errors="coerce").fillna(0)
    ppg = pd.to_numeric(df["current_avg_points"], errors="coerce")
    ppg = ppg.where(gp > 0, 0.0).fillna(0.0)
    df = df[gp >= min_games].copy()
    frame = pd.DataFrame({
        "player_id": df["player_id"],
        "player_name": df["player_name"],
        "position": df["position"],
        "proj_pts_per_game": ppg[df.index],
        "games_remaining": gp[df.index].astype(int),
    })
    frame["ros_total"] = frame["proj_pts_per_game"] * frame["games_remaining"]
    value = player_value.compute_player_value(
        frame, roster_slots=roster_slots, league_size=league_size
    )
    return value.rename(columns={
        "proj_pts_per_game": "realized_pts_per_game",
        "games_remaining": "games_played",
        "vorp_per_game": "realized_vorp_per_game",
        "vorp_ros": "realized_vorp_ros",
        "rank": "realized_rank",
    })[[
        "player_id", "player_name", "position", "realized_pts_per_game",
        "games_played", "realized_vorp_per_game", "realized_vorp_ros",
        "realized_rank",
    ]]


def _pairwise_win_rate(candidate_order, realized_by_id, window, top):
    """Among players the candidate ranked within ``window`` spots of each
    other (inside its top ``top``), fraction where the higher-ranked one
    realized more."""
    ids = list(candidate_order[:top])
    wins = total = 0
    for i, a in enumerate(ids):
        for b in ids[i + 1:i + 1 + window]:
            ra, rb = realized_by_id.get(a), realized_by_id.get(b)
            if ra is None or rb is None or ra == rb:
                continue
            total += 1
            wins += ra > rb
    return wins / total if total else np.nan


def rank_metrics(scores, realized_df, target="realized_vorp_ros", ks=DEFAULT_KS,
                 pairwise_window=PAIRWISE_WINDOW, pairwise_top=PAIRWISE_TOP):
    """Score one candidate ranking against realized value.

    ``scores`` is a Series indexed by ``player_id`` (higher = ranked better).
    Players without a score are treated as unranked (bottom of the board).
    The universe is every player with realized data.
    """
    realized = realized_df.set_index("player_id")[target].astype(float)
    universe = realized.index
    scores = pd.to_numeric(scores, errors="coerce").reindex(universe)
    scored = scores.dropna()

    out = {
        "n_scored": int(len(scored)),
        "n_universe": int(len(universe)),
    }
    if len(scored) >= 3:
        rho, _ = spearmanr(scored.values, realized.loc[scored.index].values)
        out["spearman"] = float(rho)
    else:
        out["spearman"] = np.nan

    candidate_order = scored.sort_values(ascending=False).index.tolist()
    oracle_order = realized.sort_values(ascending=False).index.tolist()
    realized_by_id = realized.to_dict()

    for k in ks:
        top_c = candidate_order[:k]
        top_o = set(oracle_order[:k])
        out[f"precision@{k}"] = len(set(top_c) & top_o) / k
        oracle_val = realized.loc[oracle_order[:k]].sum()
        cand_val = realized.loc[top_c].sum() if top_c else 0.0
        out[f"value_captured@{k}"] = (
            float(cand_val / oracle_val) if oracle_val else np.nan
        )

    out["pairwise_win_rate"] = _pairwise_win_rate(
        candidate_order, realized_by_id, pairwise_window, pairwise_top
    )
    return out


def ranking_benchmark(candidates, realized_df, targets=("realized_vorp_ros",),
                      ks=DEFAULT_KS):
    """One summary row per (candidate, target). ``candidates`` maps a name to
    a Series of scores indexed by ``player_id``."""
    # Spearman is only comparable on the same set of players, so also score
    # every candidate on the intersection of what they all ranked.
    common = None
    for scores in candidates.values():
        ids = set(pd.to_numeric(scores, errors="coerce").dropna().index)
        common = ids if common is None else common & ids
    common = sorted(common or [])

    rows = []
    for target in targets:
        common_realized = realized_df[realized_df["player_id"].isin(common)]
        for name, scores in candidates.items():
            metrics = rank_metrics(scores, realized_df, target=target, ks=ks)
            on_common = rank_metrics(scores, common_realized, target=target, ks=ks)
            metrics["spearman_common"] = on_common["spearman"]
            metrics["n_common"] = on_common["n_universe"]
            rows.append({"candidate": name, "target": target, **metrics})
    columns = SUMMARY_COLUMNS + [
        f"{m}@{k}" for k in ks for m in ("precision", "value_captured")
    ]
    return pd.DataFrame(rows, columns=columns)


def biggest_misses(engine_value_df, realized_df, inputs_df=None, n=30,
                   min_games=20, within_rank=150):
    """Players whose engine rank and realized rank disagree most, with the
    engine's inputs alongside so the cause is visible.

    Limited to players with ``min_games`` played and either rank inside
    ``within_rank``, so the table is about draftable players, not 3-game
    call-ups at the bottom of both boards.
    """
    board = engine_value_df[[
        "player_id", "player_name", "position", "rank", "proj_pts_per_game",
        "vorp_ros",
    ]].rename(columns={"rank": "engine_rank"})
    merged = board.merge(
        realized_df[[
            "player_id", "realized_rank", "realized_pts_per_game",
            "games_played", "realized_vorp_ros",
        ]],
        on="player_id", how="inner",
    )
    if inputs_df is not None:
        merged = merged.merge(
            inputs_df[["player_id", "prior_avg_points", "espn_proj_avg_points"]],
            on="player_id", how="left",
        )
    merged["rank_delta"] = merged["realized_rank"] - merged["engine_rank"]
    merged["abs_delta"] = merged["rank_delta"].abs()
    merged = merged[
        (merged["games_played"] >= min_games)
        & ((merged["engine_rank"] <= within_rank)
           | (merged["realized_rank"] <= within_rank))
    ]
    return (
        merged.sort_values("abs_delta", ascending=False)
        .head(n)
        .drop(columns="abs_delta")
        .reset_index(drop=True)
    )


def draft_scores(draft_df):
    """``player_id -> -overall_pick`` so an earlier pick scores higher. Accepts
    ``overall_pick`` or ``round_num``/``round_pick`` plus league size inferred
    from the picks per round."""
    if draft_df is None or len(draft_df) == 0:
        return pd.Series(dtype=float)
    df = draft_df.copy()
    if "overall_pick" not in df.columns:
        per_round = df.groupby("round_num")["round_pick"].max().max()
        df["overall_pick"] = (df["round_num"] - 1) * per_round + df["round_pick"]
    return -df.set_index("player_id")["overall_pick"].astype(float)


def run_preseason_backtest(inputs_df, league_size, draft_df=None,
                           roster_slots=None, min_games_per_game=20,
                           output_dir=None):
    """Replay the engine from the preseason and benchmark its board.

    ``inputs_df`` is a projection-inputs frame for a completed season, so the
    current-season columns *are* the realized outcome. Returns
    ``{"summary", "misses", "board", "realized"}``.
    """
    engine_inputs = preseason_inputs(inputs_df)
    proj = projections.project_rest_of_season(engine_inputs)
    board = player_value.compute_player_value(
        proj, roster_slots=roster_slots, league_size=league_size
    )

    realized = realized_value(inputs_df, league_size, roster_slots=roster_slots)
    # Per-game target: only players with enough games that the rate is real,
    # which separates projection skill from availability luck.
    realized_pg = realized[realized["games_played"] >= min_games_per_game]

    by_id = inputs_df.set_index("player_id")
    espn = by_id["espn_proj_avg_points"].replace(0, np.nan)
    candidates = {
        "engine_vorp": board.set_index("player_id")["vorp_ros"],
        "engine_ppg": board.set_index("player_id")["proj_pts_per_game"],
        "espn_proj": espn,
        "prior_ppg": by_id["prior_avg_points"],
    }
    if draft_df is not None and len(draft_df):
        candidates["draft_order"] = draft_scores(draft_df)

    summary = pd.concat([
        ranking_benchmark(candidates, realized, targets=("realized_vorp_ros",)),
        ranking_benchmark(candidates, realized_pg, targets=("realized_vorp_per_game",)),
    ], ignore_index=True)
    misses = biggest_misses(board, realized, inputs_df)

    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        summary.to_csv(output_dir / "backtest_ranking_summary.csv", index=False)
        misses.to_csv(output_dir / "backtest_ranking_misses.csv", index=False)
        board.merge(
            realized.drop(columns=["player_name", "position"]),
            on="player_id", how="left",
        ).to_csv(output_dir / "backtest_ranking_board.csv", index=False)

    return {"summary": summary, "misses": misses, "board": board,
            "realized": realized}
