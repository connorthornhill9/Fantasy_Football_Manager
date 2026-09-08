from ffm.news import Game, ProjectionRow, match_projections
from ffm.proposals import Proposal
from tests.test_league import PLAYERS, PlayerDB, make_ctx


def test_match_projections_by_name_and_team():
    players = PlayerDB(PLAYERS)
    rows = [
        ProjectionRow("Wide One", "KC", "WR", None, {"rec": 5, "rec_yd": 60}),
        ProjectionRow("Wide One", "BUF", "WR", None, {"rec": 1, "rec_yd": 5}),
        ProjectionRow("Run One", "KC", "RB", None, {"rush_yd": 80}),
    ]
    matched = match_projections(rows, players, ["4", "2", "6"])
    assert matched["4"] == {"rec": 5, "rec_yd": 60}
    assert matched["2"] == {"rush_yd": 80}
    assert "6" not in matched


def test_games_and_lock_validation():
    ctx = make_ctx()
    ctx._ingest_games([
        Game(home="KC", away="DEN", kickoff="2026-09-13T17:00Z", state="in", detail="Q2 5:00", weather="Sunny 70°F", indoor=False, venue="Arrowhead"),
        Game(home="MIA", away="BUF", kickoff="2026-09-14T00:20Z", state="pre", detail="9/13 - 8:20 PM EDT", weather="", indoor=False, venue="Hard Rock"),
    ])
    assert ctx.game_started("2") and not ctx.game_started("11")
    text = ctx.games_markdown()
    assert "DEN @ KC" in text and "IN PROGRESS" in text and "BUF @ MIA" in text
    current = [str(s) for s in ctx.my_roster["starters"]]
    # swapping a KC player (game in progress) is rejected
    swapped = list(current)
    swapped[1] = "10"
    errs = ctx.validate_proposal(Proposal(kind="lineup", starters=swapped, rationale="r"))
    assert any("game has started" in e for e in errs)
    # swapping the MIA WR out for an empty slot is allowed (MIA has not kicked off)
    swapped = list(current)
    swapped[6] = "0"
    errs = ctx.validate_proposal(Proposal(kind="lineup", starters=swapped, rationale="r"))
    assert not any("game has started" in e for e in errs)


def test_espn_projection_column():
    ctx = make_ctx()
    ctx._ingest_espn_projections([ProjectionRow("Free Agent", "BUF", "WR", None, {"rec": 4, "rec_yd": 50})])
    assert ctx.espn_proj["8"] == 9.0  # 4 rec + 50*0.1 under the test league's scoring
    assert "| 9.0 |" in ctx.free_agents_markdown()
