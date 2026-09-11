import asyncio
from pathlib import Path

from ffm.evaluate import Evaluator, WeekActuals
from ffm.proposals import Proposal
from ffm.store import Store
from tests.test_league import MY, make_ctx


class FakePublic:
    """Week-3 actuals: my starters scored modestly, bench WR 11 blew up, free agent 8 scored 20."""

    def __init__(self):
        self.stats = [
            {"player_id": pid, "stats": {"gp": 1, "rec": rec, "rec_yd": yd}}
            for pid, rec, yd in [("1", 0, 0), ("2", 5, 50), ("3", 2, 20), ("4", 3, 30), ("5", 1, 10), ("6", 4, 40), ("11", 10, 100), ("8", 10, 100), ("12", 0, 0), ("14", 0, 0)]
        ]
        self.matchups = [
            {"roster_id": 1, "matchup_id": 1, "points": 60.0, "starters": list(MY["starters"]), "players": list(MY["players"]) + ["8"],
             "players_points": {"1": 10.0, "2": 10.0, "3": 4.0, "4": 6.0, "5": 2.0, "6": 8.0, "11": 20.0, "8": 20.0, "12": 0.0, "14": 0.0, "10": 0.0}},
            {"roster_id": 2, "matchup_id": 1, "points": 55.0, "starters": ["9"], "players": ["9"], "players_points": {"9": 55.0}},
        ]

    async def get_matchups(self, league_id, week):
        return self.matchups

    async def get_stats(self, season, week, positions=None):
        return self.stats

    async def get_schedule(self, season, week):
        return [{"home": "KC", "away": "DEN", "status": "complete", "week": week}]


def make_store(tmp_path: Path) -> Store:
    return Store(tmp_path / "eval.sqlite3")


def test_evaluate_week_scores_moves_lineup_and_rejections(tmp_path):
    ctx = make_ctx()
    ctx._public = FakePublic()
    store = make_store(tmp_path)
    ev = Evaluator(store)

    # An executed add of free agent 8 (scored 20) who was benched; a rejected lineup that would have started 11.
    add = ctx.stamp(Proposal(kind="add", adds=["8"], rationale="r", horizon="this_week"))
    rec_add = store.add_proposal(add, None, status="executed")
    starters = list(MY["starters"]); starters[6] = "11"  # FLEX: start bench WR 11 instead of 11? current FLEX is 11 already
    starters[3] = "11"; starters[6] = "4"  # WR: 11 in for 4, 4 moves to FLEX
    lineup = ctx.stamp(Proposal(kind="lineup", starters=starters, rationale="r"))
    rec_lineup = store.add_proposal(lineup, None, status="rejected")
    drop = ctx.stamp(Proposal(kind="drop", drops=["2"], rationale="r", horizon="this_week"))
    rec_drop = store.add_proposal(drop, None, status="rejected")

    report = asyncio.run(ev.evaluate_week(ctx, 3))
    text = report.markdown()
    assert "Report card, week 3" in text
    assert "WON 60.0 to 55.0" in text
    outcomes = {o["proposal_id"]: o for o in store.outcomes(3)}
    assert outcomes[rec_add.id]["gain"] == 20.0 and outcomes[rec_add.id]["verdict"] == "advisor_right"
    assert outcomes[rec_drop.id]["gain"] == -10.0 and outcomes[rec_drop.id]["verdict"] == "good_rejection"
    assert outcomes[rec_lineup.id]["verdict"] in ("you_passed", "good_rejection")
    assert any("Free Agent scored 20.0 on the bench" in m for m in report.missed_starts)
    assert report.best_points >= report.lineup_points
    assert "Your rejections so far" in text
    assert store.latest_report()["week"] == 3


def test_lineup_only_scored_in_its_own_week(tmp_path):
    ctx = make_ctx()
    ctx._public = FakePublic()
    store = make_store(tmp_path)
    lineup = ctx.stamp(Proposal(kind="lineup", starters=list(MY["starters"]), rationale="r"))
    lineup.week = 2  # made for week 2
    store.add_proposal(lineup, None, status="executed")
    asyncio.run(Evaluator(store).evaluate_week(ctx, 3))
    assert store.outcomes(3) == []
