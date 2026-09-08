from tests.test_league import MY, make_ctx


def test_optimal_lineup_excludes_unavailable_and_reports_delta():
    ctx = make_ctx()
    ctx._ingest_projections([
        {"player_id": "2", "stats": {"pts_ppr": 10.0, "rec": 10}, "opponent": "X", "team": "KC"},
        {"player_id": "3", "stats": {"pts_ppr": 4.0, "rec": 4}, "opponent": "X", "team": "KC"},
        {"player_id": "4", "stats": {"pts_ppr": 8.0, "rec": 8}, "opponent": "X", "team": "KC"},
        {"player_id": "5", "stats": {"pts_ppr": 3.0, "rec": 3}, "opponent": "X", "team": "KC"},
        {"player_id": "11", "stats": {"pts_ppr": 9.0, "rec": 9}, "opponent": "NE", "team": "MIA"},
    ])
    starters, total = ctx.optimal_starters()
    assert len(starters) == len(ctx.starter_slots)
    assert "10" not in starters  # on IR
    # WR slots take the two best WRs (11 and 4); FLEX takes the next best RB/WR (5 or 3)
    assert starters[ctx.starter_slots.index("WR")] in ("11", "4")
    text = ctx.optimal_lineup_markdown()
    assert "Optimal total" in text and "CHANGE" in text
    summary = ctx.matchup_summary()
    assert summary["opponent"] == "Them"
    assert 0.0 <= summary["win_prob_current"] <= 1.0
    assert "Win probability" in ctx.matchup_markdown()


def test_team_needs_table():
    ctx = make_ctx()
    text = ctx.team_needs_markdown()
    assert "| 1 | Me (me) |" in text and "| 2 | Them |" in text
    assert "Strong at" in text
    assert "Positional strength" in ctx.snapshot_markdown(include_team_needs=True)
    assert "Positional strength" not in ctx.snapshot_markdown()
