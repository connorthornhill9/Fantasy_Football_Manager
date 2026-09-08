"""Injury reports and headlines from ESPN's public (undocumented, keyless) site API.

Everything here is best-effort: if ESPN changes shape or is unreachable, callers get empty results.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Iterable

import httpx

from .players import PlayerDB, normalize_name

log = logging.getLogger(__name__)

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
ESPN_FANTASY_BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"
TEAM_ABBR_FIX = {"WSH": "WAS"}  # ESPN abbreviation -> Sleeper abbreviation
HEALTHY = {"Active", "", None}

# ESPN fantasy stat ids -> Sleeper stat keys (offense only; kickers and D/ST use different buckets).
ESPN_STAT_KEYS = {
    "0": "pass_att", "1": "pass_cmp", "3": "pass_yd", "4": "pass_td", "19": "pass_2pt", "20": "pass_int",
    "23": "rush_att", "24": "rush_yd", "25": "rush_td", "26": "rush_2pt",
    "53": "rec", "58": "rec_tgt", "42": "rec_yd", "43": "rec_td", "44": "rec_2pt",
    "72": "fum_lost",
}
ESPN_POSITIONS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DEF"}


@dataclass
class Game:
    home: str
    away: str
    kickoff: str  # ISO 8601, UTC
    state: str  # pre | in | post
    detail: str
    weather: str
    indoor: bool
    venue: str

    @property
    def teams(self) -> tuple[str, str]:
        return self.home, self.away

    @property
    def started(self) -> bool:
        return self.state in ("in", "post")


@dataclass
class ProjectionRow:
    name: str
    team: str | None
    position: str | None
    injury_status: str | None
    stats: dict  # Sleeper-keyed


@dataclass
class InjuryNote:
    name: str
    team: str | None
    position: str | None
    status: str
    date: str | None
    short: str
    long: str

    @property
    def is_healthy(self) -> bool:
        return self.status in HEALTHY


@dataclass
class NewsItem:
    headline: str
    description: str
    published: str | None


class ESPNNews:
    def __init__(self, timeout: float = 20.0):
        self._client = httpx.AsyncClient(base_url=ESPN_BASE, timeout=timeout, headers={"user-agent": "fantasy-football-manager/0.1"})
        self._team_abbr: dict[str, str] | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict | None = None) -> dict | None:
        try:
            resp = await self._client.get(path, params=params)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("ESPN request %s failed: %s", path, exc)
            return None

    async def team_abbreviations(self) -> dict[str, str]:
        if self._team_abbr is not None:
            return self._team_abbr
        out: dict[str, str] = {}
        data = await self._get("/teams", {"limit": "40"})
        try:
            for entry in data["sports"][0]["leagues"][0]["teams"]:  # type: ignore[index]
                team = entry["team"]
                abbr = str(team["abbreviation"]).upper()
                out[str(team["id"])] = TEAM_ABBR_FIX.get(abbr, abbr)
        except (KeyError, TypeError, IndexError):
            log.warning("ESPN teams payload had an unexpected shape")
        self._team_abbr = out
        return out

    async def injuries(self) -> list[InjuryNote]:
        data = await self._get("/injuries")
        if not data:
            return []
        abbrs = await self.team_abbreviations()
        notes: list[InjuryNote] = []
        for team in data.get("injuries") or []:
            team_abbr = abbrs.get(str(team.get("id")))
            for item in team.get("injuries") or []:
                athlete = item.get("athlete") or {}
                pos = athlete.get("position") or {}
                notes.append(
                    InjuryNote(
                        name=str(athlete.get("displayName") or ""),
                        team=team_abbr,
                        position=pos.get("abbreviation") if isinstance(pos, dict) else None,
                        status=str(item.get("status") or ""),
                        date=item.get("date"),
                        short=str(item.get("shortComment") or ""),
                        long=str(item.get("longComment") or ""),
                    )
                )
        return notes

    async def scoreboard(self) -> list[Game]:
        """This week's games with kickoff time, status and weather."""
        data = await self._get("/scoreboard")
        games: list[Game] = []
        for event in (data or {}).get("events") or []:
            try:
                comp = event["competitions"][0]
                sides = {c["homeAway"]: str(c["team"]["abbreviation"]).upper() for c in comp["competitors"]}
                status = (comp.get("status") or {}).get("type") or {}
                weather = event.get("weather") or {}
                weather_text = ""
                if weather:
                    weather_text = str(weather.get("displayValue") or "")
                    if weather.get("temperature") is not None:
                        weather_text += f" {weather['temperature']}°F"
                venue = comp.get("venue") or {}
                games.append(
                    Game(
                        home=TEAM_ABBR_FIX.get(sides.get("home", ""), sides.get("home", "")),
                        away=TEAM_ABBR_FIX.get(sides.get("away", ""), sides.get("away", "")),
                        kickoff=str(event.get("date") or ""),
                        state=str(status.get("state") or "pre"),
                        detail=str(status.get("shortDetail") or ""),
                        weather=weather_text.strip(),
                        indoor=bool(venue.get("indoor")),
                        venue=str(venue.get("fullName") or ""),
                    )
                )
            except (KeyError, IndexError, TypeError):
                continue
        return games

    async def projections(self, season: str, week: int, limit: int = 1500) -> list[ProjectionRow]:
        """ESPN's weekly fantasy projections (offense), with stats translated to Sleeper keys."""
        abbrs = await self.team_abbreviations()
        headers = {
            "x-fantasy-filter": json.dumps(
                {
                    "players": {
                        "limit": limit,
                        "sortPercOwned": {"sortAsc": False, "sortPriority": 1},
                        "filterStatsForCurrentSeasonScoringPeriodId": {"value": [week]},
                    }
                }
            ),
            "user-agent": "fantasy-football-manager/0.1",
        }
        url = f"{ESPN_FANTASY_BASE}/seasons/{season}/segments/0/leaguedefaults/3"
        try:
            resp = await self._client.get(url, params={"scoringPeriodId": str(week), "view": "kona_player_info"}, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("ESPN projections unavailable: %s", exc)
            return []
        rows: list[ProjectionRow] = []
        for entry in data.get("players") or []:
            player = entry.get("player") or {}
            proj = next(
                (s for s in player.get("stats") or [] if s.get("statSourceId") == 1 and s.get("scoringPeriodId") == week),
                None,
            )
            if not proj:
                continue
            raw = proj.get("stats") or {}
            stats = {ESPN_STAT_KEYS[k]: float(v) for k, v in raw.items() if k in ESPN_STAT_KEYS and v}
            if not stats:
                continue
            rows.append(
                ProjectionRow(
                    name=str(player.get("fullName") or ""),
                    team=abbrs.get(str(player.get("proTeamId"))),
                    position=ESPN_POSITIONS.get(int(player.get("defaultPositionId") or 0)),
                    injury_status=player.get("injuryStatus"),
                    stats=stats,
                )
            )
        return rows

    async def news(self, limit: int = 60) -> list[NewsItem]:
        data = await self._get("/news", {"limit": str(limit)})
        if not data:
            return []
        return [
            NewsItem(
                headline=str(a.get("headline") or ""),
                description=str(a.get("description") or ""),
                published=a.get("published"),
            )
            for a in data.get("articles") or []
        ]


def match_injuries(notes: Iterable[InjuryNote], players: PlayerDB, candidate_ids: Iterable[str]) -> dict[str, InjuryNote]:
    """Map Sleeper player ids to ESPN injury notes by normalized name (+ team when it disambiguates)."""
    by_name: dict[str, list[InjuryNote]] = {}
    for note in notes:
        by_name.setdefault(normalize_name(note.name), []).append(note)
    out: dict[str, InjuryNote] = {}
    for pid in candidate_ids:
        cands = by_name.get(normalize_name(players.name(pid)))
        if not cands:
            continue
        team = players.team(pid)
        same_team = [n for n in cands if n.team == team]
        chosen = same_team[0] if same_team else (cands[0] if len(cands) == 1 else None)
        if chosen:
            out[str(pid)] = chosen
    return out


def match_projections(rows: Iterable[ProjectionRow], players: PlayerDB, candidate_ids: Iterable[str]) -> dict[str, dict]:
    """Map Sleeper player ids to ESPN projection stat lines by normalized name (+ team when it disambiguates)."""
    by_name: dict[str, list[ProjectionRow]] = {}
    for row in rows:
        by_name.setdefault(normalize_name(row.name), []).append(row)
    out: dict[str, dict] = {}
    for pid in candidate_ids:
        cands = by_name.get(normalize_name(players.name(pid)))
        if not cands:
            continue
        team = players.team(pid)
        same_team = [r for r in cands if r.team == team]
        chosen = same_team[0] if same_team else (cands[0] if len(cands) == 1 else None)
        if chosen:
            out[str(pid)] = chosen.stats
    return out


def match_news(items: Iterable[NewsItem], players: PlayerDB, candidate_ids: Iterable[str], per_player: int = 3) -> dict[str, list[NewsItem]]:
    """Headlines that mention a player by full name."""
    items = list(items)
    out: dict[str, list[NewsItem]] = {}
    for pid in candidate_ids:
        name = players.name(pid)
        if len(name) < 6:
            continue
        key = name.lower()
        hits = [it for it in items if key in it.headline.lower() or key in it.description.lower()]
        if hits:
            out[str(pid)] = hits[:per_player]
    return out
