from espn_api.basketball import League

league = League(
    league_id=608130406,
    year=2026,
    espn_s2='AECj80AOBz%2BlYAxOFREb5q8eEPiYCLShOWz%2FvMn2Oi7mADVDJh9WnwjXPZGqMEgrsLJsqDYJ0ODRkJUVibWuwZgLAIf3VDzs94cgeFreYaJ8%2B0JXz0MPXOErOk5%2Fgh%2FYVH4jg4hno5dlqPH6SOWbmho2pbMTNC4gH6MQO5e%2FLr1DZXaE5HLFstVjG3nGljywJoAEO3pG0GjgGh%2BCT3Hl16RQrSEKO8pijebpMybZJFhcftSgdCNkpOZF7iN3AjreVotTkyrvJpsfjRUcsNGRCn%2BuZlt61xPI%2Be8M9QZb0pY7VQ%3D%3D',
    swid='{911ABF90-450F-4439-99F8-EC64207B15CB}')

for team in league.teams:
    print(team)
    print(vars(team))
    break
