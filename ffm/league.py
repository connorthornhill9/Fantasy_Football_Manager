"""LeagueContext: everything the advisor needs to know about the league, gathered in one pass."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import Config, ConfigError
from .lineup import UNAVAILABLE_STATUSES, Candidate, optimal_lineup, team_distribution, win_probability
from .news import ESPNNews, Game, InjuryNote, NewsItem, ProjectionRow, match_injuries, match_news, match_projections
from .players import EMPTY_SLOT, NON_STARTING_SLOTS, PlayerDB, eligible_positions, slot_allows, slot_positions
from .proposals import Proposal
from .scoring import describe_scoring, full_scoring_markdown, score_stats
from .sleeper.auth import SleeperAuth
from .sleeper.public import SleeperPublic
from .store import Store

log = logging.getLogger(__name__)

WAIVER_TYPES = {0: "rolling waiver priority", 1: "reverse standings priority", 2: "FAAB bidding"}
LEAGUE_TYPES = {0: "redraft", 1: "keeper", 2: "dynasty"}
IR_ELIGIBLE_ALWAYS = {"IR", "PUP", "NFI"}
# Sleeper league setting -> injury status it unlocks for the IR slot
IR_OPTIONAL_STATUSES = {
    "reserve_allow_out": "Out",
    "reserve_allow_doubtful": "Doubtful",
    "reserve_allow_sus": "Sus",
    "reserve_allow_cov": "COV",
    "reserve_allow_na": "NA",
    "reserve_allow_dnr": "DNR",
}
FA_LIMITS = {"QB": 5, "RB": 10, "WR": 10, "TE": 6, "K": 3, "DEF": 3, "DL": 8, "LB": 8, "DB": 8}
SEASON_GAMES = 17


def _pts(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def _delta(value: float | None) -> str:
    return "-" if value is None else f"{value:+.1f}"


@dataclass
class PlayerLine:
    pid: str
    name: str
    pos: str
    team: str
    opp: str
    injury: str
    proj: float | None
    espn: float | None
    ros: float | None
    recent: list[float]
    trend_add: int
    trend_drop: int
    delta: float | None = None
    delta_slot: str | None = None

    @property
    def recent_avg(self) -> float | None:
        return round(sum(self.recent) / len(self.recent), 1) if self.recent else None

    def row(self, slot: str | None = None, with_delta: bool = False) -> str:
        cells: list[str] = []
        if slot is not None:
            cells.append(slot)
        cells += [f"{self.name} [{self.pid}]", self.pos, self.team, self.opp, self.injury or "", _pts(self.proj), _pts(self.espn)]
        if with_delta:
            cells.append(f"{_delta(self.delta)} ({self.delta_slot})" if self.delta is not None else "-")
        cells += [_pts(self.ros), _pts(self.recent_avg)]
        cells.append(f"+{self.trend_add}" if self.trend_add else "")
        return "| " + " | ".join(cells) + " |"


@dataclass
class LeagueContext:
    config: Config
    league: dict
    users: list[dict]
    rosters: list[dict]
    my_user: dict
    my_roster: dict
    state: dict
    players: PlayerDB
    projections: dict[str, dict] = field(default_factory=dict)
    season_proj: dict[str, float] = field(default_factory=dict)  # projected points per game, rest of season proxy
    team_opponents: dict[str, str] = field(default_factory=dict)
    recent_stats: dict[str, list[float]] = field(default_factory=dict)
    trending_add: dict[str, int] = field(default_factory=dict)
    trending_drop: dict[str, int] = field(default_factory=dict)
    matchups: list[dict] = field(default_factory=list)
    transactions: list[dict] = field(default_factory=list)
    free_agent_ids: list[str] = field(default_factory=list)
    injuries: dict[str, InjuryNote] = field(default_factory=dict)
    headlines: dict[str, list[NewsItem]] = field(default_factory=dict)
    games: dict[str, Game] = field(default_factory=dict)  # team abbreviation -> this week's game
    played_ids: set[str] = field(default_factory=set)  # players with a game played this week (Sleeper stats feed)
    espn_proj: dict[str, float] = field(default_factory=dict)  # pid -> ESPN projection under league scoring
    locked: dict[str, str] = field(default_factory=dict)
    last_report: str | None = None  # last week's report card, if one exists
    pending_claims: list[dict] = field(default_factory=list)  # my waiver claims awaiting the next run
    recent_claims: list[dict] = field(default_factory=list)  # my claims resolved in the last few days
    waiver_history: str | None = None  # e.g. "Wednesday 12:00 (67 claims), Thursday 03:00 (26 claims)"
    built_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------ construction

    @classmethod
    async def build(
        cls, config: Config, public: SleeperPublic, store: Store | None = None, auth: "SleeperAuth | None" = None
    ) -> "LeagueContext":
        config.require_sleeper()
        user = await public.get_user(config.sleeper_username)
        if not user:
            raise ConfigError(f"Sleeper user {config.sleeper_username!r} not found")
        state = await public.get_state()
        season = str(state.get("league_season") or state.get("season"))

        league_id = config.sleeper_league_id
        if not league_id:
            leagues = await public.get_user_leagues(user["user_id"], season)
            if len(leagues) == 1:
                league_id = leagues[0]["league_id"]
                log.info("Using the only %s league found: %s (%s)", season, leagues[0]["name"], league_id)
            else:
                listing = "\n".join(f"  {lg['league_id']}  {lg['name']}" for lg in leagues) or "  (none found)"
                raise ConfigError(
                    f"Set SLEEPER_LEAGUE_ID in .env. Leagues for {config.sleeper_username} in {season}:\n{listing}"
                )
        league = await public.get_league(league_id)
        if not league:
            raise ConfigError(f"League {league_id} not found")

        week = int(state.get("week") or state.get("leg") or 1)
        positions = sorted(cls._used_positions(league))
        if week > 1:
            recent = [(season, w) for w in range(max(1, week - 3), week)]
        else:  # before any games this season, use the end of last season as "recent form"
            prev = str(state.get("previous_season") or int(season) - 1)
            recent = [(prev, w) for w in (16, 17, 18)]
        tx_weeks = [w for w in range(max(1, week - 1), week + 1)]

        espn = ESPNNews() if config.espn_news else None
        try:
            (
                rosters, users, players_raw, trending_add, trending_drop, projections, season_rows, matchups,
                recent_raw, tx_raw, injury_notes, news_items, games, espn_rows, sleeper_schedule, week_stats,
            ) = await asyncio.gather(
                public.get_rosters(league_id),
                public.get_league_users(league_id),
                public.get_all_players(),
                public.get_trending("add", 24, 50),
                public.get_trending("drop", 24, 30),
                public.get_projections(season, week, positions),
                public.get_season_projections(season, positions),
                public.get_matchups(league_id, week),
                asyncio.gather(*(public.get_stats(s, w, positions) for s, w in recent)),
                asyncio.gather(*(public.get_transactions(league_id, w) for w in tx_weeks)),
                espn.injuries() if espn else _empty(),
                espn.news() if espn else _empty(),
                espn.scoreboard() if espn else _empty(),
                espn.projections(season, week) if espn else _empty(),
                public.get_schedule(season, week),
                public.get_stats(season, week, positions),
            )
        finally:
            if espn:
                await espn.aclose()

        if auth is not None:
            rosters = await cls._overlay_fresh_rosters(auth, league_id, rosters)

        my_roster = next(
            (r for r in rosters if r.get("owner_id") == user["user_id"] or user["user_id"] in (r.get("co_owners") or [])),
            None,
        )
        if not my_roster:
            raise ConfigError(f"{config.sleeper_username} does not own a roster in league {league['name']}")
        if auth is not None:
            # This week's starters live on the matchup leg (what sleeper.com shows), not the roster default.
            try:
                leg = await auth.get_matchup_leg(league_id, int(my_roster["roster_id"]), int(state.get("leg") or week))
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not read this week's matchup leg: %s", exc)
                leg = None
            if leg and leg.get("starters"):
                my_roster = dict(my_roster, starters=[str(s) for s in leg["starters"]])
                rosters = [my_roster if r is not my_roster and int(r["roster_id"]) == int(my_roster["roster_id"]) else r for r in rosters]
        pending_claims: list[dict] = []
        recent_claims: list[dict] = []
        if auth is not None:
            try:
                claims = await auth.get_transactions(league_id, types=["waiver"], limit=200)
                mine = [c for c in claims if int(my_roster["roster_id"]) in (c.get("roster_ids") or [])]
                pending_claims = [c for c in mine if c.get("status") == "pending"]
                cutoff_ms = (datetime.now(timezone.utc).timestamp() - 4 * 86400) * 1000
                recent_claims = [
                    c for c in mine
                    if c.get("status") != "pending" and int(c.get("status_updated") or c.get("created") or 0) >= cutoff_ms
                ]
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not read waiver claims: %s", exc)

        ctx = cls(
            config=config,
            league=league,
            users=users,
            rosters=rosters,
            my_user=user,
            my_roster=my_roster,
            state=state,
            players=PlayerDB(players_raw),
            trending_add={t["player_id"]: int(t.get("count", 0)) for t in trending_add},
            trending_drop={t["player_id"]: int(t.get("count", 0)) for t in trending_drop},
            matchups=matchups,
            transactions=[t for txs in tx_raw for t in txs],
            locked=store.locks() if store else {},
            last_report=((store.latest_report() or {}).get("text") if store else None),
            pending_claims=pending_claims,
            recent_claims=recent_claims,
        )
        ctx._ingest_projections(projections)
        ctx._ingest_season(season_rows)
        ctx._ingest_recent_stats(recent_raw)
        ctx._ingest_games(games)
        ctx._ingest_sleeper_schedule(sleeper_schedule)
        ctx.played_ids = {str(row.get("player_id")) for row in week_stats if (row.get("stats") or {}).get("gp")}
        ctx._compute_free_agents()
        ctx._ingest_news(injury_notes, news_items)
        ctx._ingest_espn_projections(espn_rows)
        ctx.waiver_history = await ctx._learn_waiver_cadence(public)
        ctx._public = public  # kept for follow-up reads (evaluation)
        if store is not None:
            try:  # log what we believed this week, so accuracy can be measured once the games are played
                store.upsert_projection_log(ctx.projection_log_rows())
                store.upsert_prediction(ctx.week, ctx.matchup_summary().get("win_prob_current"))
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not log projections: %s", exc)
        return ctx

    def _ingest_games(self, games: list[Game]) -> None:
        if not games and self.config.espn_news:
            log.warning("ESPN scoreboard returned no games: kickoff times and game locks are unknown for this run")
        for g in games:
            self.games[g.home] = g
            self.games[g.away] = g
            if g.home not in self.team_opponents:
                self.team_opponents[g.home] = g.away
            if g.away not in self.team_opponents:
                self.team_opponents[g.away] = g.home

    def _ingest_espn_projections(self, rows: list[ProjectionRow]) -> None:
        if not rows:
            return
        candidates = self.rostered_player_ids() | set(self.free_agent_ids)
        matched = match_projections(rows, self.players, candidates)
        scoring = self.scoring
        for pid, stats in matched.items():
            self.espn_proj[pid] = score_stats(stats, scoring) if scoring else 0.0

    def _ingest_sleeper_schedule(self, schedule: list[dict]) -> None:
        """Sleeper's schedule is the authority for game status (it is what enforces the locks); ESPN adds
        kickoff time and weather when available."""
        state_map = {"pre_game": "pre", "in_progress": "in", "complete": "post"}
        for g in schedule:
            home, away = str(g.get("home") or ""), str(g.get("away") or "")
            if not home or not away:
                continue
            state = state_map.get(str(g.get("status") or ""), "pre")
            existing = self.games.get(home) or self.games.get(away)
            if existing is not None and {existing.home, existing.away} == {home, away}:
                existing.state = state
                if not existing.detail:
                    existing.detail = str(g.get("status") or "")
                continue
            game = Game(
                home=home, away=away, kickoff=f"{g.get('date', '')}T00:00Z" if g.get("date") else "",
                state=state, detail=str(g.get("status") or ""), weather="", indoor=False, venue="",
            )
            self.games[home] = game
            self.games[away] = game
            self.team_opponents.setdefault(home, away)
            self.team_opponents.setdefault(away, home)

    def game_for(self, player_id: str) -> Game | None:
        team = self.players.team(player_id)
        return self.games.get(team) if team else None

    def game_started(self, player_id: str) -> bool:
        if str(player_id) in self.played_ids:
            return True
        g = self.game_for(player_id)
        return bool(g and g.started)

    def game_note(self, player_id: str) -> str:
        """Human line about a player's game this week, flagged when it is today or already underway."""
        from zoneinfo import ZoneInfo

        g = self.game_for(player_id)
        if g is None:
            if str(player_id) in self.played_ids:
                return "already PLAYED this week; cannot be started"
            team = self.players.team(player_id)
            return "no game this week (bye or free agent)" if team else "no team"
        tz = ZoneInfo(self.config.timezone) if self.config.timezone else None
        date_only = g.kickoff.endswith("T00:00Z")  # Sleeper's schedule has the date but not the time
        try:
            dt = datetime.fromisoformat(g.kickoff.replace("Z", "+00:00"))
            if date_only:
                when, today = dt.strftime("%a %b %d (time unknown)"), dt.date() == (datetime.now(tz) if tz else datetime.now(timezone.utc)).date()
            else:
                local = dt.astimezone(tz) if tz else dt
                when = local.strftime("%a %b %d %I:%M %p %Z").replace(" 0", " ")
                now = datetime.now(tz) if tz else datetime.now(timezone.utc)
                today = local.date() == now.date()
        except ValueError:
            when, today = g.kickoff, False
        team = self.players.team(player_id)
        opp = g.away if team == g.home else g.home
        vs = f"vs {opp}" if team == g.home else f"@ {opp}"
        if g.state == "post":
            return f"game FINISHED ({vs}); cannot be started this week"
        if g.state == "in":
            return f"game IN PROGRESS ({vs}); locked for this week"
        return f"plays {when} {vs}" + (" (TODAY)" if today else "")

    def lineup_with(self, player_id: str, slot: str, over: str | None) -> list[str]:
        """Current starters with `player_id` placed in `slot` (replacing `over`, or the slot's current occupant)."""
        starters = [str(s) for s in (self.my_roster.get("starters") or [])]
        slots = self.starter_slots
        idx = None
        if over and over in starters and slots[starters.index(over)] == slot:
            idx = starters.index(over)
        else:
            candidates = [i for i, s in enumerate(slots) if s == slot]
            empty = [i for i in candidates if starters[i] == EMPTY_SLOT]
            if empty:
                idx = empty[0]
            elif over and over in starters:
                idx = starters.index(over)
            elif candidates:
                idx = min(candidates, key=lambda i: self.proj_pts(starters[i]) or 0.0)
        if idx is None:
            raise ValueError(f"no {slot} slot in this league")
        out = list(starters)
        out[idx] = str(player_id)
        return out

    def validate_start_plan(self, player_id: str, slot: str, over: str | None) -> list[str]:
        errors: list[str] = []
        if slot not in self.starter_slots:
            return [f"{slot} is not a starting slot in this league ({', '.join(self.starter_slots)})"]
        p = self.players.get(player_id) or {}
        if not slot_allows(slot, p.get("position"), p.get("fantasy_positions")):
            errors.append(f"{self.players.label(player_id)} is not eligible for the {slot} slot")
        if over:
            starters = [str(s) for s in (self.my_roster.get("starters") or [])]
            if over not in starters:
                errors.append(f"{self.players.name(over)} is not currently a starter")
            elif self.starter_slots[starters.index(over)] != slot:
                errors.append(f"{self.players.name(over)} starts at {self.starter_slots[starters.index(over)]}, not {slot}")
            if self.game_started(over):
                errors.append(f"{self.players.name(over)} cannot be benched: his game has started")
        if self.game_started(player_id):
            errors.append(f"{self.players.name(player_id)} cannot be started: his game has started")
        return errors

    def proposal_snapshot(self, p: Proposal) -> dict:
        """Record what was known at proposal time so the decision can be judged later on its own terms."""
        ids = set(p.subject_player_ids()) | {str(s) for s in (self.my_roster.get("starters") or []) if str(s) != EMPTY_SLOT}
        return {
            "week": self.week,
            "proj": {pid: self.proj_pts(pid) for pid in ids if pid != EMPTY_SLOT},
            "espn": {pid: self.espn_proj.get(pid) for pid in ids if pid in self.espn_proj},
            "season_pg": {pid: self.season_proj.get(pid) for pid in ids if pid in self.season_proj},
            "starters": [str(s) for s in (self.my_roster.get("starters") or [])],
            "slots": self.starter_slots,
            "win_prob": (self.matchup_summary().get("win_prob_current")),
        }

    def stamp(self, p: Proposal) -> Proposal:
        p.week = self.week
        p.snapshot = self.proposal_snapshot(p)
        return p

    def projection_log_rows(self) -> list[tuple[int, str, str | None, float | None, float | None]]:
        """(week, player_id, position, sleeper_proj, espn_proj) for players worth tracking for accuracy."""
        ids: set[str] = {str(x) for x in (self.my_roster.get("players") or [])}
        opp = self.opponent_roster()
        if opp:
            ids |= {str(x) for x in (opp.get("players") or [])}
        for pos in self.used_positions:
            ids |= {line.pid for line in self.free_agents(pos, 6)}
        rows = []
        for pid in ids:
            proj = self.proj_pts(pid)
            espn = self.espn_proj.get(pid)
            if proj is None and espn is None:
                continue
            rows.append((self.week, pid, self.players.position(pid), proj, espn))
        return rows

    def suggested_start_after_add(self, player_id: str) -> tuple[str, str | None, float] | None:
        """If the optimizer would start a newly added player, return (slot, player he displaces, projected gain)."""
        if self.game_started(player_id):
            return None
        current = [str(s) for s in (self.my_roster.get("starters") or [])]
        optimal, total = self.optimal_starters()
        if player_id not in optimal:
            return None
        slot = self.starter_slots[optimal.index(player_id)]
        displaced = [s for s in current if s != EMPTY_SLOT and s not in optimal]
        gain = total - team_distribution(self.lineup_pairs(current))[0]
        return slot, (displaced[0] if displaced else None), round(gain, 1)

    async def _learn_waiver_cadence(self, public: SleeperPublic) -> str | None:
        """When did waiver claims actually process? Learned from this season so far, else last season."""
        from collections import Counter
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(self.config.timezone) if self.config.timezone else None
        for league_id in (self.league_id, self.league.get("previous_league_id")):
            if not league_id:
                continue
            try:
                txs = await public.get_season_transactions(str(league_id))
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not read transactions for %s: %s", league_id, exc)
                continue
            counter: Counter[str] = Counter()
            for t in txs:
                if t.get("type") == "waiver" and t.get("status") == "complete":
                    ts = datetime.fromtimestamp(int(t.get("status_updated") or t.get("created") or 0) / 1000, tz=tz or timezone.utc)
                    counter[ts.strftime("%A %H:00")] += 1
            this_season = str(league_id) == self.league_id
            if counter and (sum(counter.values()) >= 10 or not this_season):
                top = counter.most_common(3)
                label = "this season" if this_season else "last season"
                return ", ".join(f"{when} ({n} claims)" for when, n in top) + f" [{label}, {self.config.timezone or 'UTC'}]"
        return None

    @staticmethod
    async def _overlay_fresh_rosters(auth: "SleeperAuth", league_id: str, rosters: list[dict]) -> list[dict]:
        """The public REST rosters can lag a minute or more after a move; overlay the live GraphQL state."""
        try:
            fresh = await auth.get_league_rosters(league_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not read live rosters, using the public API: %s", exc)
            return rosters
        by_id = {int(r["roster_id"]): r for r in fresh if r.get("roster_id") is not None}
        merged = []
        for r in rosters:
            live = by_id.get(int(r.get("roster_id", -1)))
            if live:
                r = dict(r)
                for key in ("players", "starters", "reserve", "taxi"):
                    if key in live:
                        r[key] = live[key]
            merged.append(r)
        return merged

    @staticmethod
    def _used_positions(league: dict) -> set[str]:
        out: set[str] = set()
        for slot in league.get("roster_positions") or []:
            if slot not in NON_STARTING_SLOTS:
                out |= slot_positions(slot)
        return out or {"QB", "RB", "WR", "TE"}

    def _ingest_projections(self, projections: list[dict]) -> None:
        scoring = self.scoring
        for item in projections:
            pid = str(item.get("player_id") or "")
            stats = item.get("stats") or {}
            if item.get("team") and item.get("opponent"):
                self.team_opponents[str(item["team"])] = str(item["opponent"])
            if not pid or not any(k.startswith("pts_") for k in stats):
                continue  # no game projected (bye, inactive)
            pts = score_stats(stats, scoring) if scoring else float(stats.get("pts_ppr") or 0)
            self.projections[pid] = {"pts": pts, "stats": stats, "opp": item.get("opponent")}

    def _ingest_season(self, rows: list[dict]) -> None:
        scoring = self.scoring
        for item in rows:
            pid = str(item.get("player_id") or "")
            stats = item.get("stats") or {}
            if not pid or not any(k.startswith("pts_") for k in stats):
                continue
            total = score_stats(stats, scoring) if scoring else float(stats.get("pts_ppr") or 0)
            self.season_proj[pid] = round(total / SEASON_GAMES, 2)

    def _ingest_recent_stats(self, weekly: list[list[dict]]) -> None:
        scoring = self.scoring
        for week_items in weekly:
            for item in week_items:
                pid = str(item.get("player_id") or "")
                stats = item.get("stats") or {}
                if not pid or not stats.get("gp"):
                    continue
                pts = score_stats(stats, scoring) if scoring else float(stats.get("pts_ppr") or 0)
                self.recent_stats.setdefault(pid, []).append(pts)

    def _compute_free_agents(self) -> None:
        rostered = self.rostered_player_ids()
        stale_before_ms = (self.built_at.timestamp() - 365 * 86400) * 1000
        candidates = []
        for pid, p in self.players.all_fantasy_relevant(self.used_positions):
            if pid in rostered:
                continue
            proj = self.proj_pts(pid)
            ros = self.season_proj.get(pid)
            trend = self.trending_add.get(pid, 0)
            rank = int(p.get("search_rank") or 9_999_999)
            news = p.get("news_updated") or 0
            if proj is None and ros is None and trend == 0 and (news < stale_before_ms or rank >= 9_999_999):
                continue  # Sleeper's player list keeps retired players; skip anyone nobody has touched in a year
            shelved = 1 if (p.get("injury_status") in ("IR", "PUP", "NFI", "Out", "Sus")) else 0
            candidates.append((-self.value_score(proj, ros), shelved, -trend, rank, pid))
        candidates.sort()
        self.free_agent_ids = [c[-1] for c in candidates[:800]]

    def _ingest_news(self, injury_notes: list[InjuryNote], news_items: list[NewsItem]) -> None:
        if not injury_notes and not news_items:
            return
        candidates: set[str] = set(str(p) for p in (self.my_roster.get("players") or []))
        opp = self.opponent_roster()
        if opp:
            candidates |= {str(p) for p in (opp.get("players") or [])}
        for pos in self.used_positions:
            candidates |= {line.pid for line in self.free_agents(pos, FA_LIMITS.get(pos, 6))}
        self.injuries = match_injuries(injury_notes, self.players, candidates)
        self.headlines = match_news(news_items, self.players, candidates)

    @staticmethod
    def value_score(proj: float | None, ros: float | None) -> float:
        if proj is not None and ros is not None:
            return 0.5 * proj + 0.5 * ros
        return proj if proj is not None else (ros if ros is not None else 0.0)

    # ------------------------------------------------------------------ basic accessors

    @property
    def league_id(self) -> str:
        return str(self.league["league_id"])

    @property
    def league_name(self) -> str:
        return str(self.league.get("name", self.league_id))

    @property
    def my_roster_id(self) -> int:
        return int(self.my_roster["roster_id"])

    @property
    def week(self) -> int:
        return int(self.state.get("week") or self.state.get("leg") or 1)

    @property
    def leg(self) -> int:
        return int(self.state.get("leg") or self.week)

    @property
    def season(self) -> str:
        return str(self.state.get("league_season") or self.state.get("season"))

    @property
    def scoring(self) -> dict:
        return self.league.get("scoring_settings") or {}

    @property
    def settings(self) -> dict:
        return self.league.get("settings") or {}

    @property
    def roster_positions(self) -> list[str]:
        return list(self.league.get("roster_positions") or [])

    @property
    def starter_slots(self) -> list[str]:
        return [s for s in self.roster_positions if s not in NON_STARTING_SLOTS]

    @property
    def used_positions(self) -> set[str]:
        return self._used_positions(self.league)

    @property
    def active_roster_limit(self) -> int:
        return len([s for s in self.roster_positions if s not in ("IR", "TAXI")])

    @property
    def waiver_type(self) -> str:
        return WAIVER_TYPES.get(int(self.settings.get("waiver_type", 0) or 0), "unknown")

    @property
    def uses_faab(self) -> bool:
        return int(self.settings.get("waiver_type", 0) or 0) == 2

    @property
    def faab_budget(self) -> int:
        return int(self.settings.get("waiver_budget") or 0)

    @property
    def faab_remaining(self) -> int:
        used = int((self.my_roster.get("settings") or {}).get("waiver_budget_used") or 0)
        return max(0, self.faab_budget - used)

    def roster_by_id(self, roster_id: int) -> dict | None:
        return next((r for r in self.rosters if int(r.get("roster_id", -1)) == int(roster_id)), None)

    def user_by_id(self, user_id: str | None) -> dict | None:
        return next((u for u in self.users if u.get("user_id") == user_id), None)

    def owner_name(self, roster: dict) -> str:
        user = self.user_by_id(roster.get("owner_id"))
        if not user:
            return f"roster {roster.get('roster_id')}"
        team = (user.get("metadata") or {}).get("team_name")
        name = user.get("display_name") or user.get("username") or user.get("user_id")
        return f"{team} ({name})" if team and team != name else str(name)

    def rostered_player_ids(self) -> set[str]:
        out: set[str] = set()
        for r in self.rosters:
            for key in ("players", "reserve", "taxi"):
                out.update(str(p) for p in (r.get(key) or []))
        return out

    def owner_roster_of(self, player_id: str) -> int | None:
        for r in self.rosters:
            if str(player_id) in {str(p) for p in (r.get("players") or [])}:
                return int(r["roster_id"])
        return None

    def owner_label(self, player_id: str) -> str:
        owner = self.owner_roster_of(player_id)
        if owner is None:
            return "free agent"
        if owner == self.my_roster_id:
            return "MY roster"
        return self.owner_name(self.roster_by_id(owner) or {"roster_id": owner})

    def is_free_agent(self, player_id: str) -> bool:
        return str(player_id) in self.players and self.owner_roster_of(player_id) is None

    def on_my_roster(self, player_id: str) -> bool:
        return str(player_id) in {str(p) for p in (self.my_roster.get("players") or [])}

    def is_locked(self, player_id: str) -> bool:
        return str(player_id) in self.locked

    def claimed_adds(self) -> set[str]:
        """Players I already have a pending waiver claim on."""
        return {str(p) for c in self.pending_claims for p in (c.get("adds") or {})}

    def claimed_drops(self) -> set[str]:
        """Players already committed as the drop side of one of my pending claims."""
        return {str(p) for c in self.pending_claims for p in (c.get("drops") or {})}

    def pending_claims_text(self) -> str:
        if not self.pending_claims:
            return "none"
        parts = []
        for c in self.pending_claims:
            adds = ", ".join(self.players.label(p) for p in (c.get("adds") or {})) or "-"
            drops = ", ".join(self.players.label(p) for p in (c.get("drops") or {})) or "nobody"
            bid = (c.get("settings") or {}).get("waiver_bid")
            parts.append(f"add {adds}, drop {drops}" + (f", bid ${bid}" if bid is not None else ""))
        return "; ".join(parts) + " (these process at the next waiver run and may fail to a higher priority; do not re-claim them or drop their drop-side players again)"

    def recent_claims_text(self) -> str:
        if not self.recent_claims:
            return "none in the last few days"
        parts = []
        for c in sorted(self.recent_claims, key=lambda x: -(x.get("status_updated") or x.get("created") or 0)):
            adds = ", ".join(self.players.label(p) for p in (c.get("adds") or {})) or "-"
            drops = ", ".join(self.players.label(p) for p in (c.get("drops") or {})) or "nobody"
            note = (c.get("metadata") or {}).get("notes")
            status = {"complete": "WON", "failed": "LOST"}.get(str(c.get("status")), str(c.get("status")))
            parts.append(f"{status}: add {adds}, drop {drops}" + (f" ({note})" if note else ""))
        return "; ".join(parts)

    def proj_pts(self, player_id: str) -> float | None:
        entry = self.projections.get(str(player_id))
        return entry["pts"] if entry else None

    def opponent_roster(self) -> dict | None:
        mine = next((m for m in self.matchups if int(m.get("roster_id", -1)) == self.my_roster_id), None)
        if not mine:
            return None
        opp = next((m for m in self.matchups if m.get("matchup_id") == mine.get("matchup_id") and m is not mine), None)
        return self.roster_by_id(int(opp["roster_id"])) if opp else None

    def lineup_delta(self, player_id: str) -> tuple[float | None, str | None]:
        """Best projected improvement this week if this player replaced one of my starters, and in which slot."""
        proj = self.proj_pts(player_id)
        p = self.players.get(player_id)
        if proj is None or not p:
            return None, None
        best: float | None = None
        best_slot: str | None = None
        for slot, starter in self.roster_sections(self.my_roster)["starters"]:
            if not slot_allows(slot, p.get("position"), p.get("fantasy_positions")):
                continue
            current = 0.0 if starter == EMPTY_SLOT else (self.proj_pts(starter) or 0.0)
            delta = proj - current
            if best is None or delta > best:
                best, best_slot = delta, slot
        return (round(best, 1), best_slot) if best is not None else (None, None)

    def player_line(self, player_id: str, with_delta: bool = False) -> PlayerLine:
        pid = str(player_id)
        p = self.players.get(pid) or {}
        entry = self.projections.get(pid)
        opp = (entry or {}).get("opp")
        if entry is None:
            team = p.get("team")
            opp = (self.team_opponents.get(team) or "BYE") if team else "-"
        g = self.game_for(pid)
        if (g is not None and g.state == "post") or (pid in self.played_ids and not (g is not None and g.state == "in")):
            opp = f"{opp} PLAYED"
        elif g is not None and g.state == "in":
            opp = f"{opp} LIVE"
        line = PlayerLine(
            pid=pid,
            name=self.players.name(pid),
            pos=self.players.position(pid) or "?",
            team=p.get("team") or "FA",
            opp=str(opp or "-"),
            injury=self.players.injury(pid),
            proj=(entry or {}).get("pts"),
            espn=self.espn_proj.get(pid),
            ros=self.season_proj.get(pid),
            recent=self.recent_stats.get(pid, []),
            trend_add=self.trending_add.get(pid, 0),
            trend_drop=self.trending_drop.get(pid, 0),
        )
        if with_delta:
            line.delta, line.delta_slot = self.lineup_delta(pid)
        return line

    def free_agents(self, position: str | None = None, limit: int = 15, with_delta: bool = False) -> list[PlayerLine]:
        out = []
        wanted = position.upper() if position else None
        claimed = self.claimed_adds()
        for pid in self.free_agent_ids:
            if pid in claimed:
                continue  # already have a claim in for him
            if wanted:
                p = self.players.get(pid) or {}
                if wanted not in eligible_positions(p):
                    continue
            out.append(self.player_line(pid, with_delta=with_delta))
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------------ lineup optimisation / matchup

    def lineup_candidates(self, roster: dict) -> list[Candidate]:
        """Players on the active roster who could start (excludes IR/taxi and Out/Doubtful/suspended)."""
        reserve = {str(p) for p in (roster.get("reserve") or [])}
        taxi = {str(p) for p in (roster.get("taxi") or [])}
        out = []
        for pid in (roster.get("players") or []):
            pid = str(pid)
            if pid in reserve or pid in taxi:
                continue
            p = self.players.get(pid) or {}
            if p.get("injury_status") in UNAVAILABLE_STATUSES:
                continue
            out.append(Candidate(pid=pid, position=p.get("position"), fantasy_positions=list(p.get("fantasy_positions") or []), proj=self.proj_pts(pid) or 0.0))
        return out

    def optimal_starters(self, roster: dict | None = None) -> tuple[list[str], float]:
        """Best lineup by projection, honouring game locks: a player whose game has started stays where he is
        (starter or bench) and the remaining slots are optimised over players who have not played yet."""
        roster = roster or self.my_roster
        slots = self.starter_slots
        current = [str(s) for s in (roster.get("starters") or [])] + [EMPTY_SLOT] * len(slots)
        locked = {i: current[i] for i in range(len(slots)) if current[i] != EMPTY_SLOT and self.game_started(current[i])}
        candidates = [c for c in self.lineup_candidates(roster) if not self.game_started(c.pid)]
        open_slots = [s for i, s in enumerate(slots) if i not in locked]
        chosen, _ = optimal_lineup(open_slots, candidates)
        it = iter(chosen)
        starters = [locked[i] if i in locked else next(it) for i in range(len(slots))]
        total = round(sum(self.proj_pts(s) or 0.0 for s in starters if s != EMPTY_SLOT), 1)
        return starters, total

    def lineup_pairs(self, starters: list[str]) -> list[tuple[str | None, float]]:
        return [(self.players.position(s), self.proj_pts(s) or 0.0) for s in starters if s != EMPTY_SLOT]

    def matchup_summary(self) -> dict:
        """Projected totals for both lineups (current and optimal) and a heuristic win probability."""
        my_cur = [str(s) for s in (self.my_roster.get("starters") or [])]
        my_opt, _ = self.optimal_starters()
        out = {
            "my_current": team_distribution(self.lineup_pairs(my_cur))[0],
            "my_optimal": team_distribution(self.lineup_pairs(my_opt))[0],
            "opponent": None,
        }
        opp = self.opponent_roster()
        if opp:
            opp_cur = [str(s) for s in (opp.get("starters") or [])]
            opp_opt, _ = self.optimal_starters(opp)
            out.update(
                opponent=self.owner_name(opp),
                opp_current=team_distribution(self.lineup_pairs(opp_cur))[0],
                opp_optimal=team_distribution(self.lineup_pairs(opp_opt))[0],
                win_prob_current=win_probability(self.lineup_pairs(my_cur), self.lineup_pairs(opp_cur)),
                win_prob_optimal=win_probability(self.lineup_pairs(my_opt), self.lineup_pairs(opp_opt)),
            )
        return out

    def optimal_lineup_markdown(self) -> str:
        current = [str(s) for s in (self.my_roster.get("starters") or [])]
        optimal, total = self.optimal_starters()
        cur_total = team_distribution(self.lineup_pairs(current))[0]
        lines = ["| Slot | Optimal by projection | Proj | Current | Proj | |", "|---|---|---|---|---|---|"]
        changed = False
        for slot, opt, cur in zip(self.starter_slots, optimal, current + [EMPTY_SLOT] * len(optimal)):
            name_opt = "(empty)" if opt == EMPTY_SLOT else f"{self.players.name(opt)} [{opt}]"
            name_cur = "(empty)" if cur == EMPTY_SLOT else f"{self.players.name(cur)} [{cur}]"
            mark = "" if opt == cur else "CHANGE"
            changed = changed or bool(mark)
            lines.append(f"| {slot} | {name_opt} | {_pts(self.proj_pts(opt) if opt != EMPTY_SLOT else None)} | {name_cur} | {_pts(self.proj_pts(cur) if cur != EMPTY_SLOT else None)} | {mark} |")
        lines.append(f"\nOptimal total {total:.1f} vs current {cur_total:.1f} ({total - cur_total:+.1f}). "
                     + ("Players marked Out/Doubtful/IR/suspended are excluded from the optimal lineup." if changed else "The current lineup is already optimal by projection."))
        return "\n".join(lines)

    def lineup_changes(self) -> tuple[list[str], list[str], float]:
        """(players to start, players to sit, projected gain) to go from the current lineup to the optimal one."""
        current = [str(s) for s in (self.my_roster.get("starters") or []) if str(s) != EMPTY_SLOT]
        optimal, total = self.optimal_starters()
        opt = [s for s in optimal if s != EMPTY_SLOT]
        starts = [pid for pid in opt if pid not in current]
        sits = [pid for pid in current if pid not in opt]
        gain = total - team_distribution(self.lineup_pairs(current))[0]
        return starts, sits, round(gain, 1)

    def optimal_lineup_compact(self, width: int = 22) -> str:
        """Narrow fixed-width listing of the optimal lineup, sized to fit a Discord code block."""
        optimal, total = self.optimal_starters()
        current = {str(s) for s in (self.my_roster.get("starters") or [])}
        lines = []
        for slot, pid in zip(self.starter_slots, optimal):
            if pid == EMPTY_SLOT:
                lines.append(f"{slot:<5} {'(empty)':<{width}}    -")
                continue
            name = self.players.name(pid)
            name = name if len(name) <= width else name[: width - 1] + "…"
            mark = "" if pid in current else " *"
            lines.append(f"{slot:<5} {name:<{width}} {_pts(self.proj_pts(pid)):>5}{mark}")
        lines.append(f"{'Total':<5} {'':<{width}} {total:>5.1f}")
        return "\n".join(lines)

    def games_markdown(self) -> str:
        """This week's schedule with kickoff (local time), status and weather; one line per game."""
        from zoneinfo import ZoneInfo

        if not self.games:
            return (
                "WARNING: the game schedule could not be loaded, so kickoff times and game locks are UNKNOWN this run. "
                "Do not assume anyone has or has not played; say so if it matters, and prefer waiting over lineup changes "
                "that depend on it."
            )
        seen: set[str] = set()
        tz = ZoneInfo(self.config.timezone) if self.config.timezone else None
        lines = []
        for g in sorted({id(g): g for g in self.games.values()}.values(), key=lambda g: g.kickoff):
            key = f"{g.away}@{g.home}"
            if key in seen:
                continue
            seen.add(key)
            try:
                dt = datetime.fromisoformat(g.kickoff.replace("Z", "+00:00"))
                if g.kickoff.endswith("T00:00Z"):
                    when = dt.strftime("%a %b %d (kickoff time unavailable)")
                else:
                    when = dt.astimezone(tz).strftime("%a %b %d %I:%M %p %Z") if tz else dt.strftime("%a %b %d %H:%M UTC")
            except ValueError:
                when = g.kickoff
            status = {"in": " (IN PROGRESS, players locked)", "post": " (FINAL, players locked)"}.get(g.state, "")
            weather = "dome" if g.indoor else (g.weather or "weather n/a")
            lines.append(f"- {when}: {g.away} @ {g.home}, {weather}{status}")
        if not lines:
            return "(schedule unavailable)"
        mine = {self.players.team(p) for p in (self.my_roster.get("players") or [])}
        idle = sorted(t for t in mine if t and t not in self.games)
        if idle:
            lines.append(f"- Teams on bye or without a game this week: {', '.join(idle)}")
        return "\n".join(lines)

    def matchup_summary_markdown(self) -> str:
        s = self.matchup_summary()
        if not s.get("opponent"):
            return f"My projected total: {s['my_current']:.1f} (current) / {s['my_optimal']:.1f} (optimal). No opponent this week."
        return (
            f"Projected totals: me {s['my_current']:.1f} current / {s['my_optimal']:.1f} optimal vs {s['opponent']} "
            f"{s['opp_current']:.1f} current / {s['opp_optimal']:.1f} optimal. "
            f"Win probability (heuristic from projections and typical weekly variance): {s['win_prob_current']:.0%} with current lineups, "
            f"{s['win_prob_optimal']:.0%} if both sides start their optimal lineups."
        )

    # ------------------------------------------------------------------ trade analysis

    def team_needs_markdown(self) -> str:
        """Positional strength of every team relative to the league, to find trade partners."""
        positions = [p for p in ("QB", "RB", "WR", "TE", "K", "DEF", "DL", "LB", "DB") if p in self.used_positions]

        def value(pid: str) -> float:
            return self.season_proj.get(pid) or self.proj_pts(pid) or 0.0

        totals: dict[int, dict[str, float]] = {}
        counts: dict[int, dict[str, int]] = {}
        benches: dict[int, list[str]] = {}
        for r in self.rosters:
            rid = int(r["roster_id"])
            sections = self.roster_sections(r)
            totals[rid] = {p: 0.0 for p in positions}
            counts[rid] = {p: 0 for p in positions}
            for slot, pid in sections["starters"]:
                if pid == EMPTY_SLOT:
                    continue
                # Credit single-position slots to the slot (an LB starting at DL is DL production here);
                # flex slots go to the player's own position.
                pos = slot if slot in totals[rid] else self.players.position(pid)
                if pos in totals[rid]:
                    totals[rid][pos] += value(pid)
                    counts[rid][pos] += 1
            benches[rid] = sections["bench"]
        n = max(1, len(self.rosters))
        avg = {p: sum(t[p] for t in totals.values()) / n for p in positions}
        starter_avg = {p: (sum(t[p] for t in totals.values()) / max(1, sum(c[p] for c in counts.values()))) for p in positions}

        lines = ["| Roster id | Team | Record | Strong at (vs league avg) | Weak at | Notable bench pieces |", "|---|---|---|---|---|---|"]
        for r in sorted(self.rosters, key=lambda x: int(x["roster_id"])):
            rid = int(r["roster_id"])
            s = r.get("settings") or {}
            strong = [f"{p} {totals[rid][p] / avg[p]:.0%}" for p in positions if avg[p] and totals[rid][p] / avg[p] >= 1.15]
            weak = [f"{p} {totals[rid][p] / avg[p]:.0%}" for p in positions if avg[p] and totals[rid][p] / avg[p] <= 0.85]
            bench = sorted(benches[rid], key=lambda pid: -value(pid))
            notable = [
                f"{self.players.name(pid)} ({self.players.position(pid)}, {value(pid):.1f}/gm)"
                for pid in bench
                if value(pid) >= 0.8 * starter_avg.get(self.players.position(pid) or "", 999)
            ][:3]
            me = " (me)" if rid == self.my_roster_id else ""
            lines.append(
                f"| {rid} | {self.owner_name(r)}{me} | {s.get('wins', 0)}-{s.get('losses', 0)} | {', '.join(strong) or '-'} | {', '.join(weak) or '-'} | {', '.join(notable) or '-'} |"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------ lineup helpers

    def roster_sections(self, roster: dict) -> dict[str, list]:
        starters = [str(s) for s in (roster.get("starters") or [])]
        reserve = {str(p) for p in (roster.get("reserve") or [])}
        taxi = {str(p) for p in (roster.get("taxi") or [])}
        starter_set = {s for s in starters if s != EMPTY_SLOT}
        bench = [
            str(p)
            for p in (roster.get("players") or [])
            if str(p) not in starter_set and str(p) not in reserve and str(p) not in taxi
        ]
        return {
            "starters": list(zip(self.starter_slots, starters)),
            "bench": bench,
            "ir": sorted(reserve),
            "taxi": sorted(taxi),
        }

    def validate_lineup(self, starters: list[str]) -> list[str]:
        errors: list[str] = []
        slots = self.starter_slots
        if len(starters) != len(slots):
            errors.append(f"starters must have exactly {len(slots)} entries in slot order {slots}; got {len(starters)}")
            return errors
        reserve = {str(p) for p in (self.my_roster.get("reserve") or [])}
        taxi = {str(p) for p in (self.my_roster.get("taxi") or [])}
        current = [str(s) for s in (self.my_roster.get("starters") or [])]
        for slot, new, old in zip(slots, starters, current + [EMPTY_SLOT] * len(slots)):
            if str(new) == str(old):
                continue
            for pid in (str(new), str(old)):
                if pid != EMPTY_SLOT and self.game_started(pid):
                    g = self.game_for(pid)
                    errors.append(f"{self.players.name(pid)} cannot be moved: his game has started ({g.detail if g else ''})")
        seen: set[str] = set()
        for slot, pid in zip(slots, starters):
            pid = str(pid)
            if pid == EMPTY_SLOT:
                continue
            if pid in seen:
                errors.append(f"{self.players.name(pid)} appears twice")
            seen.add(pid)
            if not self.on_my_roster(pid):
                errors.append(f"{self.players.label(pid)} is not on your roster")
                continue
            if pid in reserve or pid in taxi:
                errors.append(f"{self.players.name(pid)} is on IR/taxi and cannot start")
            p = self.players.get(pid) or {}
            if not slot_allows(slot, p.get("position"), p.get("fantasy_positions")):
                errors.append(f"{self.players.label(pid)} is not eligible for the {slot} slot")
        return errors

    def validate_proposal(self, p: Proposal) -> list[str]:
        errors: list[str] = []
        for pid in p.subject_player_ids():
            if pid != EMPTY_SLOT and pid not in self.players:
                errors.append(f"unknown player id {pid!r} (use search_players to find the id)")
        if errors:
            return errors

        for pid in list(p.drops) + list(p.i_give):
            if self.is_locked(pid) and p.kind not in ("ir", "taxi", "activate_ir"):
                errors.append(f"{self.players.name(pid)} is LOCKED by the manager and cannot be dropped or traded away")
        if p.kind in ("add", "add_drop", "waiver_claim", "drop"):
            for pid in p.adds:
                if pid in self.claimed_adds():
                    errors.append(f"{self.players.name(pid)} is already in one of my pending waiver claims")
            for pid in p.drops:
                if pid in self.claimed_drops():
                    errors.append(f"{self.players.name(pid)} is already the drop in a pending waiver claim; pick a different drop")

        if p.kind in ("add", "add_drop", "waiver_claim"):
            if not p.adds:
                errors.append("adds is required")
            if p.start_slot:
                if len(p.adds) != 1:
                    errors.append("a start plan needs exactly one added player")
                else:
                    errors.extend(self.validate_start_plan(p.adds[0], p.start_slot, p.start_over))
                    if p.start_over and p.start_over in p.drops:
                        pass  # benching the player you drop is implied
            for pid in p.adds:
                if not self.is_free_agent(pid):
                    errors.append(f"{self.players.label(pid)} is not a free agent (owned by {self.owner_label(pid)})")
            for pid in p.drops:
                if not self.on_my_roster(pid):
                    errors.append(f"{self.players.label(pid)} is not on your roster, cannot drop")
            reserve = {str(x) for x in (self.my_roster.get("reserve") or [])}
            taxi = {str(x) for x in (self.my_roster.get("taxi") or [])}
            active = len([x for x in (self.my_roster.get("players") or []) if str(x) not in reserve and str(x) not in taxi])
            after = active - len(p.drops) + len(p.adds)
            if after > self.active_roster_limit:
                errors.append(
                    f"roster would have {after} active players but the limit is {self.active_roster_limit}; add a drop"
                )
            if p.kind == "waiver_claim" and self.uses_faab:
                if p.faab_bid is None:
                    errors.append("faab_bid is required in a FAAB league")
                elif p.faab_bid < 0 or p.faab_bid > self.faab_remaining:
                    errors.append(f"faab_bid must be between 0 and your remaining budget (${self.faab_remaining})")
        elif p.kind == "drop":
            if not p.drops:
                errors.append("drops is required")
            for pid in p.drops:
                if not self.on_my_roster(pid):
                    errors.append(f"{self.players.label(pid)} is not on your roster")
        elif p.kind == "lineup":
            if not p.starters:
                errors.append("starters is required for a lineup proposal")
            else:
                errors.extend(self.validate_lineup(p.starters))
                current = [str(s) for s in (self.my_roster.get("starters") or [])]
                if [str(s) for s in p.starters] == current:
                    errors.append("that is already the current lineup")
        elif p.kind in ("ir", "taxi"):
            ids = p.drops or p.adds
            if len(ids) != 1:
                errors.append("specify exactly one player_id")
            else:
                pid = ids[0]
                if not self.on_my_roster(pid):
                    errors.append(f"{self.players.label(pid)} is not on your roster")
                elif p.kind == "ir":
                    if int(self.settings.get("reserve_slots") or 0) <= 0:
                        errors.append("this league has no IR slots")
                    reserve = {str(x) for x in (self.my_roster.get("reserve") or [])}
                    if len(reserve) >= int(self.settings.get("reserve_slots") or 0):
                        errors.append("all IR slots are already used")
                    status = (self.players.get(pid) or {}).get("injury_status") or ""
                    allowed = self.ir_eligible_statuses()
                    if status not in allowed:
                        errors.append(
                            f"{self.players.name(pid)} has injury status {status or 'none'!r}; IR-eligible statuses in this league: {sorted(allowed)}"
                        )
                elif int(self.settings.get("taxi_slots") or 0) <= 0:
                    errors.append("this league has no taxi squad")
        elif p.kind == "activate_ir":
            ids = p.adds or p.drops
            reserve = {str(x) for x in (self.my_roster.get("reserve") or [])}
            if len(ids) != 1:
                errors.append("specify exactly one player_id")
            elif ids[0] not in reserve:
                errors.append(f"{self.players.label(ids[0])} is not on your IR")
        elif p.kind == "trade":
            if p.trade_partner_roster_id is None:
                errors.append("trade_partner_roster_id is required")
            else:
                partner = self.roster_by_id(p.trade_partner_roster_id)
                if not partner:
                    errors.append(f"no roster with id {p.trade_partner_roster_id}")
                elif int(partner["roster_id"]) == self.my_roster_id:
                    errors.append("cannot trade with yourself")
                else:
                    theirs = {str(x) for x in (partner.get("players") or [])}
                    for pid in p.i_get:
                        if pid not in theirs:
                            errors.append(f"{self.players.label(pid)} is not on {self.owner_name(partner)}'s roster")
            for pid in p.i_give:
                if not self.on_my_roster(pid):
                    errors.append(f"{self.players.label(pid)} is not on your roster")
            if not p.i_give and not p.i_get:
                errors.append("a trade needs at least one player on either side")
            deadline = int(self.settings.get("trade_deadline", 99) or 99)
            if deadline < 99 and self.week > deadline:
                errors.append(f"the trade deadline (week {deadline}) has passed")
        return errors

    # ------------------------------------------------------------------ markdown rendering

    ROSTER_HEADER = (
        "| Slot | Player [id] | Pos | Team | Opp | Injury | Proj wk | ESPN wk | Season proj/gm | Recent avg | Adds 24h |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|"
    )
    FA_HEADER = (
        "| Player [id] | Pos | Team | Opp | Injury | Proj wk | ESPN wk | vs my lineup (slot) | Season proj/gm | Recent avg | Adds 24h |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|"
    )

    def roster_markdown(self, roster: dict, include_bench: bool = True) -> str:
        sections = self.roster_sections(roster)
        lines = [self.ROSTER_HEADER]
        total = 0.0
        for slot, pid in sections["starters"]:
            if pid == EMPTY_SLOT:
                lines.append(f"| {slot} | (empty) | | | | | | | | | |")
                continue
            line = self.player_line(pid)
            total += line.proj or 0.0
            lines.append(line.row(slot))
        lines.append(f"\nProjected starter total: {total:.1f}")
        if include_bench:
            for label, key in (("Bench", "bench"), ("IR", "ir"), ("Taxi", "taxi")):
                if sections[key]:
                    lines.append(f"\n{label}:")
                    lines.append(self.ROSTER_HEADER)
                    for pid in sections[key]:
                        lines.append(self.player_line(pid).row(label.upper()))
        return "\n".join(lines)

    def standings_markdown(self) -> str:
        def key(r: dict):
            s = r.get("settings") or {}
            return (-int(s.get("wins", 0)), -float(s.get("fpts", 0)))

        lines = ["| Roster id | Team | Record | Points for | FAAB left |", "|---|---|---|---|---|"]
        for r in sorted(self.rosters, key=key):
            s = r.get("settings") or {}
            record = f"{s.get('wins', 0)}-{s.get('losses', 0)}" + (f"-{s['ties']}" if s.get("ties") else "")
            pf = float(s.get("fpts", 0)) + float(s.get("fpts_decimal", 0)) / 100
            faab = f"${self.faab_budget - int(s.get('waiver_budget_used') or 0)}" if self.uses_faab else "-"
            me = " (me)" if int(r["roster_id"]) == self.my_roster_id else ""
            lines.append(f"| {r['roster_id']} | {self.owner_name(r)}{me} | {record} | {pf:.1f} | {faab} |")
        return "\n".join(lines)

    def free_agents_markdown(self, per_position: dict[str, int] | None = None) -> str:
        order = ["QB", "RB", "WR", "TE", "K", "DEF", "DL", "LB", "DB"]
        per_position = per_position or {pos: FA_LIMITS.get(pos, 6) for pos in order if pos in self.used_positions}
        out = []
        for pos, limit in per_position.items():
            lines = self.free_agents(pos, limit, with_delta=True)
            if not lines:
                continue
            out.append(f"\n{pos}:\n{self.FA_HEADER}")
            out.extend(line.row(with_delta=True) for line in lines)
        return "\n".join(out)

    def transactions_markdown(self, limit: int = 15) -> str:
        txs = sorted(self.transactions, key=lambda t: t.get("created", 0), reverse=True)[:limit]
        if not txs:
            return "(no recent transactions)"
        lines = []
        for t in txs:
            when = datetime.fromtimestamp(int(t.get("created", 0)) / 1000, tz=timezone.utc).strftime("%b %d")
            teams = ", ".join(self.owner_name(self.roster_by_id(rid) or {"roster_id": rid}) for rid in (t.get("roster_ids") or []))
            adds = ", ".join(self.players.label(p) for p in (t.get("adds") or {})) or "-"
            drops = ", ".join(self.players.label(p) for p in (t.get("drops") or {})) or "-"
            bid = (t.get("settings") or {}).get("waiver_bid")
            bid_txt = f", bid ${bid}" if bid is not None else ""
            lines.append(f"- {when} {t.get('type')} ({t.get('status')}) by {teams}: adds {adds}; drops {drops}{bid_txt}")
        return "\n".join(lines)

    def matchup_markdown(self) -> str:
        roster = self.opponent_roster()
        if not roster:
            return "(no opponent this week)"
        s = roster.get("settings") or {}
        header = f"Opponent: {self.owner_name(roster)} ({s.get('wins', 0)}-{s.get('losses', 0)}), roster id {roster['roster_id']}"
        return header + "\n" + self.roster_markdown(roster, include_bench=False) + "\n\n" + self.matchup_summary_markdown()

    def team_situation_markdown(self, team: str, position: str, limit: int = 5) -> str:
        """Depth chart at one position on one NFL team, with projections and league ownership."""
        team = team.upper()
        position = position.upper()
        rows = self.players.teammates(team, position)[:limit]
        if not rows:
            return f"No {position} found on {team}."
        lines = [f"{team} {position} depth chart:"]
        for pid, p in rows:
            line = self.player_line(pid)
            depth = p.get("depth_chart_order")
            note = self.injuries.get(pid)
            espn = f"; ESPN: {note.status}, {note.short[:160]}" if note and (not note.is_healthy or note.short) else ""
            lines.append(
                f"- #{depth if depth is not None else '?'} {line.name} [{pid}] ({p.get('position')}), {self.owner_label(pid)}; "
                f"proj wk {_pts(line.proj)}, season/gm {_pts(line.ros)}, recent {_pts(line.recent_avg)}; injury: {line.injury or 'none'}{espn}"
            )
        return "\n".join(lines)

    def player_news_markdown(self, player_id: str) -> str:
        pid = str(player_id)
        p = self.players.get(pid) or {}
        parts = [f"Sleeper: status {p.get('injury_status') or 'healthy'}" + (f" ({p.get('injury_body_part')})" if p.get("injury_body_part") else "")]
        if p.get("news_updated"):
            parts[0] += f", last news update {datetime.fromtimestamp(p['news_updated'] / 1000, tz=timezone.utc):%b %d}"
        note = self.injuries.get(pid)
        if note:
            when = f" ({note.date[:10]})" if note.date else ""
            parts.append(f"ESPN injury report{when}: {note.status}. {note.short} {note.long}".strip())
        for item in self.headlines.get(pid, []):
            when = f" ({item.published[:10]})" if item.published else ""
            parts.append(f"Headline{when}: {item.headline}. {item.description}")
        if not note and not self.headlines.get(pid):
            parts.append("No ESPN injury entry or headline mentions this player.")
        return "\n".join(parts)

    def injury_report_markdown(self, max_entries: int = 14) -> str:
        """Players of interest who are not fully healthy, with ESPN commentary and the next men up."""
        groups: list[tuple[str, list[str]]] = [("my roster", [str(p) for p in (self.my_roster.get("players") or [])])]
        opp = self.opponent_roster()
        if opp:
            groups.append(("opponent", [str(s) for s in (opp.get("starters") or []) if str(s) != EMPTY_SLOT]))
        shortlist = [line.pid for pos in sorted(self.used_positions) for line in self.free_agents(pos, 4)]
        groups.append(("free agent", shortlist))

        entries: list[str] = []
        seen: set[str] = set()
        for group, ids in groups:
            for pid in ids:
                if pid in seen or len(entries) >= max_entries:
                    continue
                p = self.players.get(pid) or {}
                note = self.injuries.get(pid)
                sleeper_status = p.get("injury_status")
                if not sleeper_status and (note is None or note.is_healthy):
                    continue
                seen.add(pid)
                line = self.player_line(pid)
                text = f"- **{line.name}** [{pid}] ({line.pos}, {line.team}; {group}): Sleeper status {sleeper_status or 'healthy'}"
                if p.get("injury_body_part"):
                    text += f" ({p['injury_body_part']})"
                if note:
                    when = f", {note.date[:10]}" if note.date else ""
                    comment = note.short or note.long
                    if group == "my roster" and note.long and note.long != note.short:
                        comment = f"{note.short} {note.long}"
                    text += f". ESPN: {note.status}{when}: {comment[:420]}"
                if line.team not in ("FA", "-") and line.pos in self.used_positions:
                    mates = [
                        (mpid, mp)
                        for mpid, mp in self.players.teammates(line.team, line.pos)
                        if mpid != pid
                    ][:3]
                    if mates:
                        text += "\n  Next up at " + line.pos + ": " + "; ".join(
                            f"{self.players.name(mpid)} [{mpid}] ({self.owner_label(mpid)}, proj wk {_pts(self.proj_pts(mpid))}, season/gm {_pts(self.season_proj.get(mpid))})"
                            for mpid, _ in mates
                        )
                entries.append(text)
        return "\n".join(entries) if entries else "(no injury flags among players of interest)"

    def ir_eligible_statuses(self) -> set[str]:
        allowed = set(IR_ELIGIBLE_ALWAYS)
        for setting, status in IR_OPTIONAL_STATUSES.items():
            if self.settings.get(setting):
                allowed.add(status)
        return allowed

    def waiver_rules_text(self) -> str:
        s = self.settings
        my = self.my_roster.get("settings") or {}
        parts = [f"Waivers: {self.waiver_type}"]
        if self.uses_faab:
            parts.append(f"FAAB budget ${self.faab_budget}, I have ${self.faab_remaining} left")
        else:
            parts.append(
                f"my waiver priority is {my.get('waiver_position', '?')} of {self.league.get('total_rosters')}"
                + ("; winning a claim sends me to the bottom of the priority list" if int(s.get("waiver_type", 0) or 0) == 0 else "")
            )
        if s.get("daily_waivers"):
            hour = s.get("daily_waivers_hour")
            parts.append(f"waivers run on league-selected days at {hour}:00 Pacific" if hour is not None else "waivers run daily on league-selected days")
        parts.append(f"dropped players stay on waivers for {s.get('waiver_clear_days', '?')} day(s)")
        if self.waiver_history:
            parts.append(f"claims actually processed at: {self.waiver_history}")
        if self.config.league_notes:
            parts.append(f"manager's notes on this league's rules: {self.config.league_notes}")
        return "; ".join(parts) + "."

    def rules_markdown(self) -> str:
        s = self.settings
        my = self.my_roster.get("settings") or {}
        record = f"{my.get('wins', 0)}-{my.get('losses', 0)}" + (f"-{my['ties']}" if my.get("ties") else "")
        deadline = int(s.get("trade_deadline", 99) or 99)
        locked = ", ".join(f"{name} [{pid}]" for pid, name in self.locked.items()) or "none"
        lines = [
            f"League: {self.league_name} ({self.league.get('total_rosters')} teams, {LEAGUE_TYPES.get(int(s.get('type', 0) or 0), 'redraft')}), season {self.season}, week {self.week}",
            f"Scoring summary: {describe_scoring(self.scoring)}",
            f"All scoring rules: {full_scoring_markdown(self.scoring)}",
            f"Starting slots (in order): {', '.join(self.starter_slots)}",
            f"Bench slots: {self.roster_positions.count('BN')}, IR slots: {s.get('reserve_slots', 0)} (Sleeper statuses allowed on IR here: {', '.join(sorted(self.ir_eligible_statuses()))}; note 'NA' = Commissioner's Exempt / not active, 'Sus' = suspended), taxi slots: {s.get('taxi_slots', 0)}, active roster limit: {self.active_roster_limit}",
            self.waiver_rules_text(),
            f"Trade deadline: {'none' if deadline >= 99 else f'week {deadline}'}; playoffs start week {s.get('playoff_week_start', '?')}",
            f"My team: {self.owner_name(self.my_roster)} (roster id {self.my_roster_id}), record {record}, points for {float(my.get('fpts', 0)):.1f}",
            f"LOCKED players (the manager forbids dropping or trading these; you may still start/bench them and may say what you would do if they were unlocked): {locked}",
            f"My pending waiver claims: {self.pending_claims_text()}",
            f"My recent waiver claim results: {self.recent_claims_text()}",
        ]
        return "\n".join(lines)

    def snapshot_markdown(self, include_team_needs: bool = False) -> str:
        parts = [
            "# League and team snapshot",
            self.rules_markdown(),
            "\n## My roster",
            "Columns: Opp = this week's opponent; 'PLAYED' or 'LIVE' after it means the player's game is over or under way, "
            "so he is LOCKED for the week: he cannot be moved into or out of the lineup and is not a fallback for later games. "
            "Proj wk = Sleeper/Rotowire projection this week under league scoring ('-' = no projected production); "
            "ESPN wk = ESPN's projection under the same scoring (a second opinion; offense only); "
            "Season proj/gm = preseason full-season projection per game (a rest-of-season value proxy); "
            f"Recent avg = last 3 games played{' (end of last season)' if self.week <= 1 else ''}; Adds 24h = adds across all Sleeper leagues.",
            self.roster_markdown(self.my_roster),
            "\n## Optimal lineup by projection (computed; use your judgement on Questionable players and news)",
            self.optimal_lineup_markdown(),
            "\n## This week's matchup",
            self.matchup_markdown(),
            "\n## This week's games (kickoff, weather)",
            self.games_markdown(),
        ]
        if include_team_needs:
            parts += ["\n## Positional strength by team (for trade targeting)", self.team_needs_markdown()]
        if self.last_report:
            parts += ["\n## Your track record (last report card; learn from the misses, do not overreact to one week's variance)", self.last_report]
        parts += [
            "\n## Injury and availability notes (Sleeper status + ESPN injury report)",
            self.injury_report_markdown(),
            "\n## Top available free agents by position",
            "'vs my lineup' = projected gain this week if the player replaced my weakest eligible starter (negative = no lineup upgrade this week).",
            self.free_agents_markdown(),
            "\n## Trending drops league-wide (24h)",
            ", ".join(
                f"{self.players.label(pid)} ({n})"
                for pid, n in sorted(self.trending_drop.items(), key=lambda kv: -kv[1])[:10]
                if pid in self.players
            )
            or "(none)",
            "\n## Recent league transactions",
            self.transactions_markdown(),
            "\n## Standings",
            self.standings_markdown(),
        ]
        return "\n".join(parts)


async def _empty() -> list:
    return []
