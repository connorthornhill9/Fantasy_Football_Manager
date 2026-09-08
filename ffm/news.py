"""Injury reports and headlines from ESPN's public (undocumented, keyless) site API.

Everything here is best-effort: if ESPN changes shape or is unreachable, callers get empty results.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

import httpx

from .players import PlayerDB, normalize_name

log = logging.getLogger(__name__)

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
TEAM_ABBR_FIX = {"WSH": "WAS"}  # ESPN abbreviation -> Sleeper abbreviation
HEALTHY = {"Active", "", None}


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
