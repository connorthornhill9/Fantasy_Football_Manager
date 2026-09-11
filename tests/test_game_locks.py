from ffm.news import Game
from tests.test_league import MY, make_ctx


def _ctx_with_played_bench_wr():
    ctx = make_ctx()
    ctx._ingest_projections([
        {"player_id": "4", "stats": {"pts_ppr": 6.0, "rec": 6}, "opponent": "DEN", "team": "KC"},
        {"player_id": "5", "stats": {"pts_ppr": 5.0, "rec": 5}, "opponent": "DEN", "team": "KC"},
        {"player_id": "11", "stats": {"pts_ppr": 14.0, "rec": 14}, "opponent": "NE", "team": "MIA"},  # bench WR, big number
    ])
    # MIA already played (final); KC plays later
    ctx._ingest_games([
        Game(home="MIA", away="NE", kickoff="2026-09-10T00:20Z", state="post", detail="Final", weather="", indoor=False, venue=""),
        Game(home="KC", away="DEN", kickoff="2026-09-13T20:25Z", state="pre", detail="9/13 - 4:25 PM EDT", weather="", indoor=False, venue=""),
    ])
    return ctx


def test_played_players_are_marked_in_tables():
    ctx = _ctx_with_played_bench_wr()
    assert "NE PLAYED" in ctx.player_line("11").opp
    assert ctx.player_line("4").opp == "DEN"
    assert "LOCKED for the week" in ctx.snapshot_markdown()


def test_optimizer_never_moves_a_played_bench_player_in():
    ctx = _ctx_with_played_bench_wr()
    # 11 is on the bench in the fixture (starters WR slots hold 4 and 5); he has played, so he must stay benched
    # even though his projection beats both starters.
    ctx.my_roster["starters"] = ["1", "2", "3", "4", "5", "6", "0", "12", "0", "14"]
    optimal, _ = ctx.optimal_starters()
    assert "11" not in optimal
    ctx.my_roster["starters"] = list(MY["starters"])


def test_optimizer_keeps_a_played_starter_in_place():
    ctx = _ctx_with_played_bench_wr()
    # put the played MIA player in the FLEX slot as a starter; he must stay there
    ctx.my_roster["starters"] = ["1", "2", "3", "4", "5", "6", "11", "12", "0", "14"]
    optimal, _ = ctx.optimal_starters()
    assert optimal[ctx.starter_slots.index("FLEX")] == "11"
    ctx.my_roster["starters"] = list(MY["starters"])
