"""Lineup optimisation and matchup win probability, computed from projections (no model involved)."""
from __future__ import annotations

import math
from dataclasses import dataclass

from .players import EMPTY_SLOT, slot_allows

# Typical week-to-week coefficient of variation (std dev / mean) of fantasy scoring by position.
# Rough figures from historical weekly scoring; used only for the win-probability heuristic.
POSITION_CV = {"QB": 0.35, "RB": 0.50, "WR": 0.55, "TE": 0.60, "K": 0.50, "DEF": 0.60, "DL": 0.55, "LB": 0.45, "DB": 0.50}
DEFAULT_CV = 0.50
UNAVAILABLE_STATUSES = {"Out", "Doubtful", "IR", "PUP", "NFI", "Sus", "COV", "NA"}


@dataclass
class Candidate:
    pid: str
    position: str | None
    fantasy_positions: list[str]
    proj: float


def optimal_lineup(slots: list[str], candidates: list[Candidate]) -> tuple[list[str], float]:
    """Assign candidates to slots to maximise total projected points (Hungarian algorithm).

    Returns the starters list in slot order ("0" for an unfilled slot) and the total.
    """
    n_slots = len(slots)
    n_cands = len(candidates)
    if n_slots == 0:
        return [], 0.0
    # Square cost matrix: rows = slots, cols = candidates (+ dummy columns so every slot can stay empty).
    size = max(n_slots, n_cands + n_slots)
    big = 10_000.0
    cost = [[big] * size for _ in range(size)]
    for i, slot in enumerate(slots):
        for j, c in enumerate(candidates):
            if slot_allows(slot, c.position, c.fantasy_positions):
                cost[i][j] = -c.proj
        for j in range(n_cands, size):
            cost[i][j] = 0.0  # leave the slot empty
    for i in range(n_slots, size):
        for j in range(size):
            cost[i][j] = 0.0  # dummy slots
    assignment = _hungarian(cost)
    starters: list[str] = []
    total = 0.0
    for i in range(n_slots):
        j = assignment[i]
        if j < n_cands and cost[i][j] < big:
            starters.append(candidates[j].pid)
            total += candidates[j].proj
        else:
            starters.append(EMPTY_SLOT)
    return starters, round(total, 1)


def _hungarian(cost: list[list[float]]) -> list[int]:
    """Minimum-cost perfect assignment on a square matrix. Returns row -> column."""
    n = len(cost)
    u = [0.0] * (n + 1)
    v = [0.0] * (n + 1)
    p = [0] * (n + 1)
    way = [0] * (n + 1)
    inf = float("inf")
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [inf] * (n + 1)
        used = [False] * (n + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = inf
            j1 = 0
            for j in range(1, n + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]
                        j1 = j
            for j in range(n + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    result = [0] * n
    for j in range(1, n + 1):
        if p[j]:
            result[p[j] - 1] = j - 1
    return result


def team_distribution(players: list[tuple[str | None, float]]) -> tuple[float, float]:
    """Mean and standard deviation of a lineup's total from (position, projection) pairs."""
    mean = sum(proj for _, proj in players)
    var = sum((proj * POSITION_CV.get(pos or "", DEFAULT_CV)) ** 2 for pos, proj in players)
    return round(mean, 1), round(math.sqrt(var), 1)


def win_probability(mine: list[tuple[str | None, float]], theirs: list[tuple[str | None, float]]) -> float:
    """P(my total > their total) assuming independent normal totals."""
    my_mean, my_sd = team_distribution(mine)
    op_mean, op_sd = team_distribution(theirs)
    sd = math.sqrt(my_sd**2 + op_sd**2)
    if sd == 0:
        return 1.0 if my_mean > op_mean else (0.5 if my_mean == op_mean else 0.0)
    z = (my_mean - op_mean) / sd
    return round(0.5 * (1 + math.erf(z / math.sqrt(2))), 3)
