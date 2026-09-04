"""Player value over replacement (VORP) from rest-of-season projections.

Turns a projection table (see :mod:`features.projections`) into a single value
number that ranks a draft board and a waiver list the same way.

Pure function, no network access. See ``docs/predictive_engine.md``.
"""

import numpy as np
import pandas as pd

# Starting-lineup template for a standard ESPN H2H points league. Keys are the
# roster slots; values are how many of each start.
DEFAULT_ROSTER_SLOTS = {
    "PG": 1, "SG": 1, "SF": 1, "PF": 1, "C": 1, "G": 1, "F": 1, "UT": 3,
}

# Which primary positions satisfy each flex slot.
_SLOT_ELIGIBILITY = {
    "PG": {"PG"}, "SG": {"SG"}, "SF": {"SF"}, "PF": {"PF"}, "C": {"C"},
    "G": {"PG", "SG"}, "F": {"SF", "PF"},
    "UT": {"PG", "SG", "SF", "PF", "C"},
}

PRIMARY_POSITIONS = ("PG", "SG", "SF", "PF", "C")

OUTPUT_COLUMNS = [
    "rank",
    "player_id",
    "player_name",
    "position",
    "eligible_positions",
    "proj_pts_per_game",
    "games_remaining",
    "ros_total",
    "replacement_pts_per_game",
    "vorp_per_game",
    "vorp_ros",
]


def parse_positions(position):
    """``"PG/SG"`` -> ``["PG", "SG"]``; unknown / empty -> ``[]``."""
    if position is None or (isinstance(position, float) and np.isnan(position)):
        return []
    tokens = str(position).replace(",", "/").replace(" ", "/").split("/")
    return [t.upper() for t in tokens if t.upper() in PRIMARY_POSITIONS]


def _replacement_index(slots, league_size):
    """Number of players rostered as starters at each primary position.

    Flex slots are spread evenly across the positions they accept, which is a
    deliberate simplification: good enough to set a replacement line, exact
    enough allocation is not worth it here.
    """
    counts = {pos: 0.0 for pos in PRIMARY_POSITIONS}
    for slot, n in slots.items():
        eligible = _SLOT_ELIGIBILITY.get(slot, set())
        if not eligible:
            continue
        share = n / len(eligible)
        for pos in eligible:
            counts[pos] += share
    return {pos: int(round(c * league_size)) for pos, c in counts.items()}


def compute_player_value(projections_df, roster_slots=None, league_size=10,
                         output_dir=None):
    """Rank players by projected value over replacement.

    ``projections_df`` is the output of
    :func:`features.projections.project_rest_of_season`.
    """
    slots = dict(roster_slots) if roster_slots else dict(DEFAULT_ROSTER_SLOTS)

    if projections_df is None or len(projections_df) == 0:
        result = pd.DataFrame(columns=OUTPUT_COLUMNS)
        if output_dir is not None:
            result.to_csv(output_dir / "player_value.csv", index=False)
        return result

    df = projections_df.copy()
    df = df[df["proj_pts_per_game"].notna()].copy()
    df["eligible_positions"] = df["position"].apply(parse_positions)

    repl_idx = _replacement_index(slots, league_size)

    # Per-position replacement line: the Nth-best projected per-game rate among
    # players eligible there, N from repl_idx.
    replacement_pg = {}
    for pos in PRIMARY_POSITIONS:
        eligible = df[df["eligible_positions"].apply(lambda ps: pos in ps)]
        rates = eligible["proj_pts_per_game"].sort_values(ascending=False)
        n = repl_idx.get(pos, 0)
        if len(rates) == 0 or n <= 0:
            replacement_pg[pos] = np.nan
        elif n >= len(rates):
            replacement_pg[pos] = float(rates.iloc[-1])
        else:
            replacement_pg[pos] = float(rates.iloc[n - 1])

    global_repl = np.nanmin(list(replacement_pg.values())) if replacement_pg else np.nan

    def _player_replacement(positions):
        vals = [replacement_pg[p] for p in positions
                if p in replacement_pg and np.isfinite(replacement_pg[p])]
        # Best (highest) positional line the player can be slotted against; if
        # they have no known position, fall back to the global line.
        return max(vals) if vals else global_repl

    df["replacement_pts_per_game"] = df["eligible_positions"].apply(
        _player_replacement
    ).round(3)
    df["vorp_per_game"] = (
        df["proj_pts_per_game"] - df["replacement_pts_per_game"]
    ).round(3)
    df["vorp_ros"] = (df["vorp_per_game"] * df["games_remaining"]).round(1)
    df["eligible_positions"] = df["eligible_positions"].apply(
        lambda ps: "/".join(ps)
    )

    result = df.sort_values("vorp_ros", ascending=False, na_position="last")
    result = result.reset_index(drop=True)
    result["rank"] = result.index + 1

    for col in OUTPUT_COLUMNS:
        if col not in result.columns:
            result[col] = np.nan
    result = result[OUTPUT_COLUMNS]

    if output_dir is not None:
        result.to_csv(output_dir / "player_value.csv", index=False)

    return result
