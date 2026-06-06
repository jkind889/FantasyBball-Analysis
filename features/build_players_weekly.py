import pandas as pd

def build_players_weekly(league,start_week,end_week):
    player_rows = []

    for week in range(start_week, end_week + 1):
        box_scores = league.box_scores(matchup_period=week)

        for box in box_scores:

            # Home team players
            for player in box.home_lineup:
                player_rows.append({
                    "week": week,
                    "fantasy_team_id": box.home_team.team_id,
                    "fantasy_team": box.home_team.team_name,
                    "opponent_team_id": box.away_team.team_id,
                    "opponent_team": box.away_team.team_name,

                    "player_id": player.playerId,
                    "player_name": player.name,
                    "position": player.position,
                    "lineup_slot": player.lineupSlot,
                    "eligible_slots": player.eligibleSlots,

                    "points": player.points,

                    "was_started": player.lineupSlot not in ["BE", "IR"],
                    "pro_team": player.proTeam,
                    "injury_status": player.injuryStatus,
                    "injured": player.injured
                })

            # Away team players
            for player in box.away_lineup:
                player_rows.append({
                    "week": week,
                    "fantasy_team_id": box.away_team.team_id,
                    "fantasy_team": box.away_team.team_name,
                    "opponent_team_id": box.home_team.team_id,
                    "opponent_team": box.home_team.team_name,

                    "player_id": player.playerId,
                    "player_name": player.name,
                    "position": player.position,
                    "lineup_slot": player.lineupSlot,
                    "eligible_slots": player.eligibleSlots,

                    "points": player.points,

                    "was_started": player.lineupSlot not in ["BE", "IR"],
                    "pro_team": player.proTeam,
                    "injury_status": player.injuryStatus,
                    "injured": player.injured
                })
    
    return pd.DataFrame(player_rows)