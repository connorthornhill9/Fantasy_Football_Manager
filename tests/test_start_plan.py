from ffm.news import Game
from ffm.proposals import Proposal
from tests.test_league import MY, make_ctx


def test_start_plan_validation_and_lineup_with():
    ctx = make_ctx()
    ctx._ingest_projections([{"player_id": "8", "stats": {"pts_ppr": 12.0, "rec": 6, "rec_yd": 70}, "opponent": "NYJ", "team": "BUF"}])
    # add free agent 8 (WR) and start him at WR over 4
    p = Proposal(kind="add", adds=["8"], rationale="r", horizon="this_week", start_slot="WR", start_over="4")
    assert ctx.validate_proposal(p) == []
    starters = ctx.lineup_with("8", "WR", "4")
    assert starters[ctx.starter_slots.index("WR")] == "8" and "4" not in starters
    # wrong slot for a WR, and a start_over who is not in that slot
    assert any("not eligible for the QB" in e for e in ctx.validate_proposal(Proposal(kind="add", adds=["8"], rationale="r", horizon="this_week", start_slot="QB")))
    assert any("starts at QB, not WR" in e for e in ctx.validate_proposal(Proposal(kind="add", adds=["8"], rationale="r", horizon="this_week", start_slot="WR", start_over="1")))
    assert any("not a starting slot" in e for e in ctx.validate_proposal(Proposal(kind="add", adds=["8"], rationale="r", horizon="this_week", start_slot="BN")))
    assert p.has_start_plan and not Proposal(kind="add", adds=["8"], rationale="r").has_start_plan


def test_start_plan_blocked_once_game_started_and_game_note():
    ctx = make_ctx()
    ctx._ingest_games([Game(home="BUF", away="MIA", kickoff="2026-09-13T17:00Z", state="in", detail="Q1", weather="", indoor=False, venue="")])
    p = Proposal(kind="add", adds=["8"], rationale="r", horizon="this_week", start_slot="WR", start_over="4")
    assert any("his game has started" in e for e in ctx.validate_proposal(p))
    assert "IN PROGRESS" in ctx.game_note("8")
    assert "no game this week" in ctx.game_note("2")  # KC has no game in this fixture


def test_suggested_start_after_add():
    ctx = make_ctx()
    ctx._ingest_projections([
        {"player_id": "8", "stats": {"pts_ppr": 12.0, "rec": 6, "rec_yd": 70}, "opponent": "NYJ", "team": "BUF"},
        {"player_id": "11", "stats": {"pts_ppr": 5.0, "rec": 2, "rec_yd": 20}, "opponent": "NE", "team": "MIA"},
    ])
    # pretend the add landed: 8 is now on my roster (bench)
    ctx.my_roster["players"] = list(MY["players"]) + ["8"]
    slot, over, gain = ctx.suggested_start_after_add("8")
    assert slot in ("WR", "FLEX") and gain > 0
    ctx.my_roster["players"] = list(MY["players"])
    assert ctx.suggested_start_after_add("13") is None  # not on roster, not projected into the lineup
