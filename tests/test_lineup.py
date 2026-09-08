from ffm.lineup import Candidate, optimal_lineup, team_distribution, win_probability


def cand(pid, pos, proj, fps=None):
    return Candidate(pid=pid, position=pos, fantasy_positions=fps or [pos], proj=proj)


def test_optimal_lineup_uses_flex_for_best_leftover():
    slots = ["QB", "RB", "RB", "WR", "TE", "FLEX"]
    cands = [
        cand("q1", "QB", 20), cand("q2", "QB", 25),
        cand("r1", "RB", 12), cand("r2", "RB", 10), cand("r3", "RB", 9),
        cand("w1", "WR", 15), cand("w2", "WR", 14),
        cand("t1", "TE", 6),
    ]
    starters, total = optimal_lineup(slots, cands)
    assert starters == ["q2", "r1", "r2", "w1", "t1", "w2"]
    assert total == 25 + 12 + 10 + 15 + 6 + 14


def test_optimal_lineup_leaves_slot_empty_when_no_eligible_player():
    starters, total = optimal_lineup(["QB", "K"], [cand("q1", "QB", 18)])
    assert starters == ["q1", "0"] and total == 18


def test_optimal_lineup_idp_and_dual_eligibility():
    slots = ["DL", "LB"]
    cands = [cand("a", "LB", 10, ["DL", "LB"]), cand("b", "DE", 8, ["DL"]), cand("c", "LB", 7)]
    starters, total = optimal_lineup(slots, cands)
    assert set(starters) == {"a", "b"} and total == 18


def test_win_probability_symmetry_and_direction():
    mine = [("QB", 20), ("RB", 12), ("WR", 14)]
    theirs = [("QB", 20), ("RB", 12), ("WR", 14)]
    assert win_probability(mine, theirs) == 0.5
    better = [("QB", 25), ("RB", 15), ("WR", 18)]
    p = win_probability(better, theirs)
    assert 0.6 < p < 0.95
    assert abs(win_probability(theirs, better) - (1 - p)) < 0.002
    mean, sd = team_distribution(mine)
    assert mean == 46.0 and sd > 0
