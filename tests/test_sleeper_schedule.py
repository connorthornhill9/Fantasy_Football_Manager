from ffm.news import Game
from tests.test_league import make_ctx


def test_sleeper_schedule_is_authority_for_status():
    ctx = make_ctx()
    # ESPN says KC-DEN is pre-game with a kickoff time; Sleeper says it is complete
    ctx._ingest_games([Game(home="KC", away="DEN", kickoff="2026-09-13T20:25Z", state="pre", detail="", weather="Sunny", indoor=False, venue="")])
    ctx._ingest_sleeper_schedule([
        {"home": "KC", "away": "DEN", "date": "2026-09-13", "status": "complete", "week": 1},
        {"home": "MIA", "away": "BUF", "date": "2026-09-14", "status": "pre_game", "week": 1},  # not in ESPN's list
    ])
    assert ctx.games["KC"].state == "post" and ctx.games["KC"].kickoff == "2026-09-13T20:25Z"  # ESPN time kept
    assert ctx.games["MIA"].state == "pre" and ctx.games["MIA"].kickoff == "2026-09-14T00:00Z"
    assert ctx.game_started("2") and not ctx.game_started("11")
    assert "kickoff time unavailable" in ctx.games_markdown()
    assert "time unknown" in ctx.game_note("11")


def test_played_ids_lock_players_without_any_schedule():
    ctx = make_ctx()
    ctx.played_ids = {"11"}
    assert ctx.game_started("11") and not ctx.game_started("4")
    assert "PLAYED" in ctx.player_line("11").opp
    assert "already PLAYED" in ctx.game_note("11")
    optimal, _ = ctx.optimal_starters()
    assert "11" not in optimal or ctx.my_roster["starters"][ctx.starter_slots.index("FLEX")] == "11"
