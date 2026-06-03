import pandas as pd

def build_matchups(league,start_week,end_week):
    matchup_rows = []

    for week in range(start_week, end_week + 1):  # Assuming a 22-week season
        box_scores = league.box_scores(matchup_period=week)

        for box in box_scores:
            home_score = box.home_score
            away_score = box.away_score

            if home_score > away_score:
                winner = box.home_team.team_name
                loser = box.away_team.team_name
            elif away_score > home_score:
                winner = box.away_team.team_name
                loser = box.home_team.team_name
            else:
                winner = "Tie"
                loser = "Tie"

            matchup_rows.append({
                "week": week,

                "home_team_id": box.home_team.team_id,
                "home_team": box.home_team.team_name,
                "home_score": home_score,

                "away_team_id": box.away_team.team_id,
                "away_team": box.away_team.team_name,
                "away_score": away_score,

                "winner": winner,
                "loser": loser,
                "margin": abs(home_score - away_score)
            })

    matchups_df = pd.DataFrame(matchup_rows)
    return matchups_df