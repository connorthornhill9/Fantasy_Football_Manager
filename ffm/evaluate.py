"""Weekly self-evaluation: score past proposals against what actually happened, with no model involved.

Two kinds of finding:
- process errors (an added player who out-scored a starter while benched; points left on the bench), which are
  actionable immediately, and
- judgment quality (realized gain of approved and rejected moves, projection-source accuracy, win-probability
  calibration), which only means something over many weeks.

Rejections are scored exactly like approvals; the verdict is written from the manager's side of the decision.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .league import LeagueContext
from .lineup import Candidate, optimal_lineup
from .players import EMPTY_SLOT, slot_allows
from .proposals import Proposal
from .scoring import score_stats
from .store import ProposalRecord, Store

log = logging.getLogger(__name__)

FOLLOW_WEEKS = 3  # how many weeks after the move to keep scoring it
VERDICTS = {
    "advisor_right": "advisor right, you approved",
    "advisor_wrong": "advisor wrong, you approved",
    "you_passed": "YOU PASSED on this",
    "good_rejection": "good rejection",
    "not_executed": "not executed (would have been)",
    "no_data": "no data yet",
}


@dataclass
class WeekActuals:
    week: int
    points: dict[str, float]  # player_id -> actual points under league scoring (only players who have played)
    my_starters: list[str]
    my_players: list[str]
    my_points: float
    opp_points: float | None
    opponent: str | None
    complete: bool = True  # every game of the week is final


@dataclass
class WeekReport:
    week: int
    lineup_points: float = 0.0
    best_points: float = 0.0
    best_changes: list[str] = field(default_factory=list)
    move_lines: list[str] = field(default_factory=list)
    missed_starts: list[str] = field(default_factory=list)
    accuracy_lines: list[str] = field(default_factory=list)
    record_line: str = ""
    override_line: str = ""
    complete: bool = True

    @property
    def left_on_bench(self) -> float:
        return round(self.best_points - self.lineup_points, 1)

    def markdown(self) -> str:
        eff = f"{self.lineup_points / self.best_points:.0%}" if self.best_points else "n/a"
        parts = [
            f"**Report card, week {self.week}**" + ("" if self.complete else " (week still in progress; partial numbers)"),
            f"Lineup: {self.lineup_points:.1f} scored vs {self.best_points:.1f} best possible from the roster ({eff}); {self.left_on_bench:+.1f} left on the bench."
            + (" Better: " + "; ".join(self.best_changes) if self.best_changes else ""),
        ]
        if self.record_line:
            parts.append(self.record_line)
        parts.append("Moves: " + ("\n" + "\n".join(f"- {m}" for m in self.move_lines) if self.move_lines else "none evaluated this week"))
        if self.missed_starts:
            parts.append("Missed starts (process error): " + "; ".join(self.missed_starts))
        if self.accuracy_lines:
            parts.append("Projection accuracy so far: " + "; ".join(self.accuracy_lines))
        if self.override_line:
            parts.append(self.override_line)
        return "\n".join(parts)


class Evaluator:
    def __init__(self, store: Store):
        self.store = store

    # ------------------------------------------------------------------ data gathering

    async def week_actuals(self, ctx: LeagueContext, week: int) -> WeekActuals:
        """Actual points for a finished week: Sleeper's own scoring for rostered players, league scoring for the rest."""
        public = ctx_public(ctx)
        matchups = await public.get_matchups(ctx.league_id, week)
        stats = await public.get_stats(ctx.season, week, sorted(ctx.used_positions))
        schedule = await public.get_schedule(ctx.season, week)
        complete = bool(schedule) and all(str(g.get("status")) == "complete" for g in schedule)
        points: dict[str, float] = {}
        for row in stats:
            pid = str(row.get("player_id") or "")
            st = row.get("stats") or {}
            if pid and st.get("gp"):
                points[pid] = score_stats(st, ctx.scoring)
        mine = next((m for m in matchups if int(m.get("roster_id", -1)) == ctx.my_roster_id), None)
        opp = None
        if mine:
            opp = next((m for m in matchups if m.get("matchup_id") == mine.get("matchup_id") and m is not mine), None)
            for pid, pts in (mine.get("players_points") or {}).items():
                if str(pid) in points:  # played: Sleeper's own number beats my recomputation
                    points[str(pid)] = float(pts)
        return WeekActuals(
            week=week,
            points=points,
            my_starters=[str(s) for s in ((mine or {}).get("starters") or [])],
            my_players=[str(p) for p in ((mine or {}).get("players") or [])],
            my_points=float((mine or {}).get("points") or 0.0),
            opp_points=float(opp["points"]) if opp and opp.get("points") is not None else None,
            opponent=ctx.owner_name(ctx.roster_by_id(int(opp["roster_id"])) or {"roster_id": opp["roster_id"]}) if opp else None,
            complete=complete,
        )

    # ------------------------------------------------------------------ scoring one proposal

    @staticmethod
    def _sum(points: dict[str, float], ids: list[str]) -> float:
        return round(sum(points.get(str(p), 0.0) for p in ids if p != EMPTY_SLOT), 1)

    def score_proposal(self, rec: ProposalRecord, actuals: WeekActuals, names) -> tuple[float | None, float | None, float | None, str, str] | None:
        """(gain, side_a, side_b, verdict, detail) for one proposal in one week, or None if not scoreable."""
        p = rec.proposal
        pts = actuals.points
        if p.kind in ("add", "add_drop", "waiver_claim"):
            a, b = self._sum(pts, p.adds), self._sum(pts, p.drops)
            detail = f"added {', '.join(names(x) for x in p.adds)} scored {a:.1f}" + (f"; dropped {', '.join(names(x) for x in p.drops)} scored {b:.1f}" if p.drops else "")
        elif p.kind == "drop":
            a, b = 0.0, self._sum(pts, p.drops)
            detail = f"dropped {', '.join(names(x) for x in p.drops)} scored {b:.1f}"
        elif p.kind == "lineup" and p.starters and p.snapshot and p.snapshot.get("starters"):
            if actuals.week != p.week:
                return None  # a lineup only applies to its own week
            a, b = self._sum(pts, p.starters), self._sum(pts, p.snapshot["starters"])
            detail = f"proposed lineup {a:.1f} vs lineup at the time {b:.1f}"
        elif p.kind == "trade":
            a, b = self._sum(pts, p.i_get), self._sum(pts, p.i_give)
            detail = f"would receive {a:.1f}, would send {b:.1f}"
        else:
            return None
        gain = round(a - b, 1)
        if rec.status == "executed":
            verdict = "advisor_right" if gain > 0 else "advisor_wrong"
        elif rec.status == "rejected":
            verdict = "you_passed" if gain > 0 else "good_rejection"
        else:
            verdict = "not_executed"
        return gain, a, b, verdict, detail

    # ------------------------------------------------------------------ the weekly pass

    async def evaluate_week(self, ctx: LeagueContext, week: int) -> WeekReport:
        names = ctx.players.name
        actuals = await self.week_actuals(ctx, week)
        report = WeekReport(week=week)

        # 1. Lineup efficiency: what was scored vs the best lineup the roster could have fielded.
        candidates = [
            Candidate(pid=pid, position=ctx.players.raw_position(pid), fantasy_positions=list((ctx.players.get(pid) or {}).get("fantasy_positions") or []), proj=actuals.points.get(pid, 0.0))
            for pid in actuals.my_players
        ]
        best, best_total = optimal_lineup(ctx.starter_slots, candidates)
        report.complete = actuals.complete
        report.lineup_points = self._sum(actuals.points, actuals.my_starters)
        report.best_points = round(best_total, 1)
        for slot, was, could in zip(ctx.starter_slots, actuals.my_starters + [EMPTY_SLOT] * len(best), best):
            if was != could and could != EMPTY_SLOT and actuals.points.get(could, 0) > actuals.points.get(was, 0) + 1:
                report.best_changes.append(f"{slot}: {names(could)} ({actuals.points.get(could, 0):.1f}) over {names(was) if was != EMPTY_SLOT else 'empty'} ({actuals.points.get(was, 0):.1f})")
        self.store.set_week_result(week, actuals.my_points, actuals.opp_points, report.lineup_points, report.best_points)
        preds = {r["week"]: r for r in self.store.week_predictions()}
        pred = preds.get(week) or {}
        if actuals.opp_points is not None:
            won = actuals.my_points > actuals.opp_points
            wp = pred.get("win_prob")
            report.record_line = (
                f"Result: {'WON' if won else 'LOST'} {actuals.my_points:.1f} to {actuals.opp_points:.1f} vs {actuals.opponent}"
                + (f" (win probability at the time {wp:.0%})" if wp is not None else "")
            )

        # 2. Every proposal made for this week or the few before it.
        overrides = {"good": 0, "bad": 0}
        for rec in self.store.all_proposals():
            p = rec.proposal
            if p.week is None or not (week - FOLLOW_WEEKS <= p.week <= week):
                continue
            if rec.status in ("pending", "expired", "advice") and p.kind != "trade":
                continue
            scored = self.score_proposal(rec, actuals, names)
            if scored is None:
                continue
            gain, a, b, verdict, detail = scored
            self.store.upsert_outcome(rec.id, week, gain, a, b, verdict, detail)
            if p.week == week:
                report.move_lines.append(f"#{rec.id} {p.title(names)}: {gain:+.1f} ({detail}). {VERDICTS[verdict]}.")
            if rec.status == "rejected":
                overrides["good" if gain <= 0 else "bad"] += 1
            # 3. Process error: an executed add who out-scored a starter at an eligible slot while benched.
            if rec.status == "executed" and p.kind in ("add", "add_drop", "waiver_claim") and p.week == week:
                for pid in p.adds:
                    if pid in actuals.my_players and pid not in actuals.my_starters:
                        player = ctx.players.get(pid) or {}
                        for slot, starter in zip(ctx.starter_slots, actuals.my_starters):
                            if slot_allows(slot, player.get("position"), player.get("fantasy_positions")) and actuals.points.get(pid, 0) > actuals.points.get(starter, 0) + 2:
                                report.missed_starts.append(f"{names(pid)} scored {actuals.points.get(pid, 0):.1f} on the bench while {names(starter)} scored {actuals.points.get(starter, 0):.1f} at {slot}")
                                break

        # 4. Projection accuracy (cumulative): fill in actuals for the logged projections once the week is complete.
        if actuals.complete:
            self.store.set_projection_actuals(week, {pid: pts for pid, pts in actuals.points.items()})
        report.accuracy_lines = self.accuracy_lines(week)

        # 5. Override tally, cumulative.
        allo = self.store.outcomes()
        good = sum(1 for o in allo if o["verdict"] == "good_rejection")
        bad = sum(1 for o in allo if o["verdict"] == "you_passed")
        if good or bad:
            report.override_line = f"Your rejections so far: {good} good, {bad} that would have helped (you passed on {sum(o['gain'] for o in allo if o['verdict'] == 'you_passed'):+.1f} points in total)."

        text = report.markdown()
        if actuals.complete:
            self.store.save_report(week, text)  # only a finished week becomes the model's track record
        return report

    def accuracy_lines(self, up_to_week: int) -> list[str]:
        rows = self.store.projection_log(up_to_week)
        by_pos: dict[str, dict[str, list[float]]] = {}
        for r in rows:
            if r["actual"] is None:
                continue
            bucket = by_pos.setdefault(r["position"] or "?", {"sleeper": [], "espn": []})
            if r["sleeper_proj"] is not None:
                bucket["sleeper"].append(abs(r["sleeper_proj"] - r["actual"]))
            if r["espn_proj"] is not None:
                bucket["espn"].append(abs(r["espn_proj"] - r["actual"]))
        lines = []
        for pos in sorted(by_pos):
            s, e = by_pos[pos]["sleeper"], by_pos[pos]["espn"]
            if len(s) < 3:
                continue
            line = f"{pos}: Sleeper off by {sum(s) / len(s):.1f} on average (n={len(s)})"
            if len(e) >= 3:
                line += f", ESPN off by {sum(e) / len(e):.1f}"
            lines.append(line)
        return lines


def ctx_public(ctx: LeagueContext):
    """The public client the context was built with (kept on the context for follow-up reads)."""
    public = getattr(ctx, "_public", None)
    if public is None:
        raise RuntimeError("context has no public client attached")
    return public
