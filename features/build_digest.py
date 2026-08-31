def _closest_and_biggest_blowout(matchups_df, week):
    week_matchups = matchups_df[matchups_df["week"] == week]
    decided = week_matchups[week_matchups["winner"] != "Tie"]

    if decided.empty:
        return None, None

    closest = decided.loc[decided["margin"].idxmin()]
    blowout = decided.loc[decided["margin"].idxmax()]
    return closest, blowout


def _top_riser(breakout_players_df):
    if breakout_players_df.empty:
        return None
    return breakout_players_df.loc[breakout_players_df["avg_points_jump"].idxmax()]


def _best_value_pick(draft_analysis_df):
    scored = draft_analysis_df.dropna(subset=["vope_score"])
    if scored.empty:
        return None
    return scored.loc[scored["vope_score"].idxmax()]


def _upset_of_the_week(biggest_upsets_df, week):
    if biggest_upsets_df is None or biggest_upsets_df.empty:
        return None
    week_upsets = biggest_upsets_df[biggest_upsets_df["week"] == week]
    if week_upsets.empty:
        return None
    return week_upsets.loc[week_upsets["avg_gap"].idxmax()]


def build_digest(
    matchups_df,
    breakout_players_df,
    draft_analysis_df,
    season_year,
    week=None,
    biggest_upsets_df=None,
):
    if week is None and not matchups_df.empty:
        week = matchups_df["week"].max()

    lines = [f"Fantasy Basketball Weekly Digest — {season_year}, Week {week}", ""]

    closest, blowout = (None, None)
    if week is not None:
        closest, blowout = _closest_and_biggest_blowout(matchups_df, week)

    if closest is not None:
        lines.append(
            f"Closest matchup: {closest['home_team']} {closest['home_score']:.1f} - "
            f"{closest['away_score']:.1f} {closest['away_team']} (margin {closest['margin']:.1f})"
        )
    if blowout is not None:
        lines.append(
            f"Biggest blowout: {blowout['winner']} beat {blowout['loser']} by {blowout['margin']:.1f}"
        )

    if week is not None:
        upset = _upset_of_the_week(biggest_upsets_df, week)
        if upset is not None:
            lines.append(
                f"Upset of the week: {upset['winner']} "
                f"(avg {upset['winner_season_avg_entering_week']:.1f}) beat "
                f"{upset['loser']} (avg {upset['loser_season_avg_entering_week']:.1f})"
            )

    top_riser = _top_riser(breakout_players_df)
    if top_riser is not None:
        lines.append(
            f"Top riser: {top_riser['player_name']} (+{top_riser['avg_points_jump']:.1f} avg points "
            f"vs. last season)"
        )

    best_pick = _best_value_pick(draft_analysis_df)
    if best_pick is not None:
        lines.append(
            f"Best draft value so far: {best_pick['player_name']} ({best_pick['fantasy_team']}), "
            f"vope score {best_pick['vope_score']:.1f}"
        )

    subject = f"Fantasy Basketball Digest — Week {week}" if week is not None else "Fantasy Basketball Digest"
    body = "\n".join(lines)
    return subject, body
