from pathlib import Path

from ffm.config import Config
from ffm.league import LeagueContext
from ffm.news import InjuryNote, NewsItem
from ffm.players import PlayerDB
from ffm.proposals import Proposal


def player(pid, name, pos, team="KC", **extra):
    first, last = name.split(" ", 1)
    return dict(
        player_id=pid, full_name=name, first_name=first, last_name=last, position=pos, team=team, active=True,
        fantasy_positions=[extra.pop("fantasy_position", pos)], **extra
    )


PLAYERS = {
    "1": player("1", "Quarter Back", "QB"),
    "2": player("2", "Run One", "RB", depth_chart_order=1),
    "3": player("3", "Run Two", "RB"),
    "4": player("4", "Wide One", "WR"),
    "5": player("5", "Wide Two", "WR"),
    "6": player("6", "Tight End", "TE"),
    "7": player("7", "Kick Er", "K"),
    "DEN": dict(player_id="DEN", first_name="Denver", last_name="Broncos", position="DEF", team="DEN", active=True),
    "8": player("8", "Free Agent", "WR", team="BUF", search_rank=50),
    "9": player("9", "Their Guy", "RB", team="DAL"),
    "10": player("10", "Hurt Back", "RB", injury_status="IR", injury_body_part="Knee"),
    "11": player("11", "Bench Wide", "WR", team="MIA"),
    "12": player("12", "Edge Rusher", "DE", team="LV", fantasy_position="DL"),
    "13": player("13", "Free Backer", "LB", team="SEA", search_rank=300),
    "14": player("14", "Corner Back", "CB", team="PHI", fantasy_position="DB"),
    "15": player("15", "Backup Back", "RB", depth_chart_order=2, search_rank=400),
}

LEAGUE = {
    "league_id": "L",
    "name": "Test League",
    "total_rosters": 2,
    "roster_positions": ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "DL", "LB", "DB", "BN", "IR"],
    "scoring_settings": {"rec": 1, "rec_yd": 0.1, "idp_tkl_solo": 1.5, "idp_sack": 3},
    "settings": {"waiver_type": 2, "waiver_budget": 100, "reserve_slots": 1, "taxi_slots": 0, "trade_deadline": 13},
}
MY = {
    "roster_id": 1,
    "owner_id": "me",
    "players": ["1", "2", "3", "4", "5", "6", "10", "11", "12", "14"],
    "starters": ["1", "2", "3", "4", "5", "6", "11", "12", "0", "14"],
    "reserve": ["10"],
    "taxi": [],
    "settings": {"wins": 1, "losses": 0, "fpts": 100, "waiver_budget_used": 30},
}
THEM = {"roster_id": 2, "owner_id": "them", "players": ["9"], "starters": ["9"], "settings": {"wins": 0, "losses": 1, "fpts": 80}}


def make_ctx(locked: dict | None = None) -> LeagueContext:
    config = Config(
        sleeper_username="me", sleeper_league_id="L", sleeper_token=None, discord_bot_token=None,
        discord_channel_id=None, discord_owner_id=None, discord_guild_id=None, model="m", effort="high",
        analysis_cron="0 9 * * 2", lineup_cron="0 9 * * 0", news_cron="0 18 * * 6", timezone=None,
        data_dir=Path("."), dry_run=True, max_proposals=6, web_search=False, espn_news=False,
    )
    ctx = LeagueContext(
        config=config, league=LEAGUE, users=[{"user_id": "me", "display_name": "Me"}, {"user_id": "them", "display_name": "Them"}],
        rosters=[MY, THEM], my_user={"user_id": "me"}, my_roster=MY,
        state={"week": 3, "leg": 3, "season": "2026", "league_season": "2026"}, players=PlayerDB(PLAYERS),
        matchups=[{"roster_id": 1, "matchup_id": 1}, {"roster_id": 2, "matchup_id": 1}],
        locked=locked or {},
    )
    ctx._ingest_projections([
        {"player_id": "8", "stats": {"pts_ppr": 12.0, "rec": 6, "rec_yd": 70}, "opponent": "NYJ", "team": "BUF"},
        {"player_id": "11", "stats": {"pts_ppr": 5.0, "rec": 2, "rec_yd": 20}, "opponent": "NE", "team": "MIA"},
        {"player_id": "13", "stats": {"pts_ppr": 9.0, "idp_tkl_solo": 4, "idp_sack": 1}, "opponent": "SF", "team": "SEA"},
        {"player_id": "999", "stats": {"adp_dd_ppr": 1000.0}},
    ])
    ctx._ingest_season([
        {"player_id": "8", "stats": {"pts_ppr": 170.0, "rec": 85, "rec_yd": 850}},
        {"player_id": "15", "stats": {"pts_ppr": 34.0, "rec": 17, "rec_yd": 170}},
    ])
    ctx._compute_free_agents()
    ctx._ingest_news(
        [InjuryNote("Hurt Back", "KC", "RB", "Injured Reserve", "2026-09-07T00:00Z", "Out for the year.", "Torn ACL in camp.")],
        [NewsItem("Run One shines in camp", "Run One is the clear starter", "2026-09-06T00:00Z")],
    )
    return ctx


def errors(ctx, **kwargs):
    return ctx.validate_proposal(Proposal(rationale="r", **kwargs))


def test_basics():
    ctx = make_ctx()
    assert ctx.my_roster_id == 1
    assert ctx.starter_slots == ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "DL", "LB", "DB"]
    assert ctx.used_positions == {"QB", "RB", "WR", "TE", "DL", "LB", "DB"}
    assert ctx.active_roster_limit == 11
    assert ctx.uses_faab and ctx.faab_remaining == 70
    assert ctx.is_free_agent("8") and not ctx.is_free_agent("9")
    assert ctx.proj_pts("8") == 13.0  # league scoring: 6 rec + 70*0.1
    assert ctx.proj_pts("13") == 9.0  # 4*1.5 + 3
    assert ctx.proj_pts("999") is None
    assert ctx.season_proj["8"] == 10.0  # 170 / 17
    assert [l.pid for l in ctx.free_agents("WR")] == ["8"]
    assert [l.pid for l in ctx.free_agents("LB")] == ["13"]
    assert [l.pid for l in ctx.free_agents("RB")] == ["15"]  # season projection alone keeps him relevant
    assert ctx.opponent_roster()["roster_id"] == 2


def test_lineup_delta_and_fa_table():
    ctx = make_ctx()
    delta, slot = ctx.lineup_delta("8")
    assert (delta, slot) == (13.0, "WR")  # WR starters have no projection, so the full 13 is the gain
    assert ctx.lineup_delta("13") == (9.0, "LB")  # empty LB slot
    text = ctx.free_agents_markdown()
    assert "+13.0 (WR)" in text and "LB:" in text and "K:" not in text


def test_news_sections():
    ctx = make_ctx()
    assert ctx.injuries["10"].status == "Injured Reserve"
    report = ctx.injury_report_markdown()
    assert "Hurt Back" in report and "Torn ACL" in report
    assert "Next up at RB" in report and "Run One" in report
    assert "Run One shines" in ctx.player_news_markdown("2")
    assert "Backup Back" in ctx.team_situation_markdown("KC", "RB")


def test_add_validation():
    ctx = make_ctx()
    assert errors(ctx, kind="add", adds=["8"]) == []  # 9 active + 1 = 10 < 11
    assert any("not a free agent" in e for e in errors(ctx, kind="add", adds=["9"]))
    assert any("unknown player" in e for e in errors(ctx, kind="add", adds=["nope"]))
    assert any("not on your roster" in e for e in errors(ctx, kind="add_drop", adds=["8"], drops=["9"]))
    assert any("limit" in e for e in errors(ctx, kind="add", adds=["8", "13", "15"]))


def test_locks_block_drops_and_trades_but_not_lineups():
    ctx = make_ctx(locked={"6": "Tight End"})
    assert any("LOCKED" in e for e in errors(ctx, kind="add_drop", adds=["8"], drops=["6"]))
    assert any("LOCKED" in e for e in errors(ctx, kind="drop", drops=["6"]))
    assert any("LOCKED" in e for e in errors(ctx, kind="trade", trade_partner_roster_id=2, i_give=["6"], i_get=["9"]))
    assert errors(ctx, kind="add_drop", adds=["8"], drops=["11"]) == []
    current = list(MY["starters"])
    swapped = current[:5] + ["0"] + current[6:]
    assert errors(ctx, kind="lineup", starters=swapped) == []
    assert "Tight End [6]" in ctx.rules_markdown()


def test_waiver_validation():
    ctx = make_ctx()
    assert any("faab_bid is required" in e for e in errors(ctx, kind="waiver_claim", adds=["8"], drops=["11"]))
    assert any("remaining budget" in e for e in errors(ctx, kind="waiver_claim", adds=["8"], drops=["11"], faab_bid=71))
    assert errors(ctx, kind="waiver_claim", adds=["8"], drops=["11"], faab_bid=15) == []


def test_lineup_validation():
    ctx = make_ctx()
    assert any("exactly 10" in e for e in errors(ctx, kind="lineup", starters=["1", "2"]))
    current = list(MY["starters"])
    assert any("already the current" in e for e in errors(ctx, kind="lineup", starters=current))
    bad_flex = current[:6] + ["1"] + current[7:]  # QB in FLEX and duplicated
    errs = errors(ctx, kind="lineup", starters=bad_flex)
    assert any("not eligible for the FLEX" in e for e in errs) and any("twice" in e for e in errs)
    ir_start = current[:6] + ["10"] + current[7:]
    assert any("IR/taxi" in e for e in errors(ctx, kind="lineup", starters=ir_start))
    wrong_idp = current[:7] + ["14", "0", "12"]  # DB in DL slot, DE in DB slot
    errs = errors(ctx, kind="lineup", starters=wrong_idp)
    assert any("not eligible for the DL" in e for e in errs) and any("not eligible for the DB" in e for e in errs)


def test_ir_eligibility_follows_league_flags():
    ctx = make_ctx()
    assert ctx.ir_eligible_statuses() == {"IR", "PUP", "NFI"}
    ctx.players.get("2")["injury_status"] = "NA"
    assert any("injury status 'NA'" in e for e in errors(ctx, kind="ir", drops=["2"]))
    ctx.league["settings"]["reserve_allow_na"] = 1
    assert "NA" in ctx.ir_eligible_statuses()
    ctx.my_roster["reserve"] = []  # free the single IR slot for this check
    assert errors(ctx, kind="ir", drops=["2"]) == []
    ctx.my_roster["reserve"] = ["10"]
    assert any("IR slots are already used" in e for e in errors(ctx, kind="ir", drops=["2"]))
    del ctx.league["settings"]["reserve_allow_na"]
    del ctx.players.get("2")["injury_status"]


def test_ir_and_trade_validation():
    ctx = make_ctx()
    assert any("injury status" in e for e in errors(ctx, kind="ir", drops=["2"]))
    assert errors(ctx, kind="activate_ir", adds=["10"]) == []
    assert any("not on your IR" in e for e in errors(ctx, kind="activate_ir", adds=["2"]))
    assert any("no taxi" in e for e in errors(ctx, kind="taxi", drops=["2"]))
    assert errors(ctx, kind="trade", trade_partner_roster_id=2, i_give=["3"], i_get=["9"]) == []
    assert any("not on Them" in e for e in errors(ctx, kind="trade", trade_partner_roster_id=2, i_get=["8"]))
    assert any("yourself" in e for e in errors(ctx, kind="trade", trade_partner_roster_id=1, i_get=["2"]))


def test_snapshot_renders():
    ctx = make_ctx(locked={"6": "Tight End"})
    text = ctx.snapshot_markdown()
    assert "Test League" in text and "Free Agent [8]" in text and "Hurt Back" in text
    assert "FAAB budget $100, I have $70 left" in text
    assert "idp_sack=3" in text and "LOCKED players" in text
