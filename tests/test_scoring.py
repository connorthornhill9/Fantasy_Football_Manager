from ffm.scoring import describe_scoring, score_stats


def test_score_stats_dot_product():
    stats = {"rec": 5, "rec_yd": 50, "rush_td": 1, "unused": 99}
    scoring = {"rec": 1, "rec_yd": 0.1, "rush_td": 6, "pass_yd": 0.04}
    assert score_stats(stats, scoring) == 16.0


def test_score_stats_handles_missing():
    assert score_stats(None, {"rec": 1}) == 0.0
    assert score_stats({"rec": 3}, None) == 0.0
    assert score_stats({"rec": "bad"}, {"rec": 1}) == 0.0


def test_describe_scoring():
    assert "full PPR" in describe_scoring({"rec": 1, "pass_td": 4, "rush_td": 6})
    assert "0.5 PPR" in describe_scoring({"rec": 0.5, "pass_td": 6, "rush_td": 6})
    assert "standard" in describe_scoring({"rec": 0})
