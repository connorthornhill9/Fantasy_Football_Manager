"""Read-only Sleeper API client (documented REST endpoints plus the projections/stats feeds)."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://api.sleeper.app"
FANTASY_POSITIONS = ["QB", "RB", "WR", "TE", "K", "DEF", "DL", "LB", "DB"]
USER_AGENT = "fantasy-football-manager/0.1 (personal, non-commercial)"


class SleeperAPIError(RuntimeError):
    pass


class SleeperPublic:
    """Async client for the public, unauthenticated Sleeper API."""

    def __init__(self, data_dir: Path, timeout: float = 30.0):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=timeout,
            headers={"user-agent": USER_AGENT, "accept": "application/json"},
        )
        self._players_cache: dict[str, Any] | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "SleeperPublic":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ transport

    async def _get(self, path: str, params: list[tuple[str, str]] | dict | None = None, retries: int = 3) -> Any:
        delay = 1.0
        last_error: Exception | None = None
        for attempt in range(retries):
            try:
                resp = await self._client.get(path, params=params)
            except httpx.HTTPError as exc:
                last_error = exc
            else:
                if resp.status_code == 404:
                    return None
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_error = SleeperAPIError(f"HTTP {resp.status_code} for {path}")
                else:
                    resp.raise_for_status()
                    return resp.json()
            if attempt < retries - 1:
                await asyncio.sleep(delay)
                delay *= 2
        raise SleeperAPIError(f"Request failed for {path}: {last_error}")

    # ------------------------------------------------------------------ users / leagues

    async def get_user(self, username_or_id: str) -> dict | None:
        return await self._get(f"/v1/user/{username_or_id}")

    async def get_user_leagues(self, user_id: str, season: str, sport: str = "nfl") -> list[dict]:
        return await self._get(f"/v1/user/{user_id}/leagues/{sport}/{season}") or []

    async def get_league(self, league_id: str) -> dict | None:
        return await self._get(f"/v1/league/{league_id}")

    async def get_rosters(self, league_id: str) -> list[dict]:
        return await self._get(f"/v1/league/{league_id}/rosters") or []

    async def get_league_users(self, league_id: str) -> list[dict]:
        return await self._get(f"/v1/league/{league_id}/users") or []

    async def get_matchups(self, league_id: str, week: int) -> list[dict]:
        return await self._get(f"/v1/league/{league_id}/matchups/{week}") or []

    async def get_transactions(self, league_id: str, week: int) -> list[dict]:
        return await self._get(f"/v1/league/{league_id}/transactions/{week}") or []

    async def get_traded_picks(self, league_id: str) -> list[dict]:
        return await self._get(f"/v1/league/{league_id}/traded_picks") or []

    async def get_season_transactions(self, league_id: str, weeks: int = 18) -> list[dict]:
        """Every transaction of a league season (cached per process; used to learn the waiver cadence)."""
        cache = getattr(self, "_season_tx_cache", None)
        if cache is None:
            cache = self._season_tx_cache = {}
        if league_id not in cache:
            weekly = await asyncio.gather(*(self.get_transactions(league_id, w) for w in range(1, weeks + 1)))
            cache[league_id] = [t for txs in weekly for t in txs]
        return cache[league_id]

    async def get_state(self, sport: str = "nfl") -> dict:
        state = await self._get(f"/v1/state/{sport}")
        if not state:
            raise SleeperAPIError("Could not fetch NFL state")
        return state

    # ------------------------------------------------------------------ players

    async def get_trending(self, kind: str = "add", lookback_hours: int = 24, limit: int = 50) -> list[dict]:
        return (
            await self._get(
                f"/v1/players/nfl/trending/{kind}",
                params={"lookback_hours": str(lookback_hours), "limit": str(limit)},
            )
            or []
        )

    async def get_all_players(self, max_age_hours: float = 24.0) -> dict[str, dict]:
        """Full NFL player dictionary (~5 MB). Cached on disk and refreshed at most daily."""
        if self._players_cache is not None:
            return self._players_cache
        cache_file = self.data_dir / "players_nfl.json"
        if cache_file.exists():
            age_hours = (time.time() - cache_file.stat().st_mtime) / 3600
            if age_hours < max_age_hours:
                try:
                    self._players_cache = json.loads(cache_file.read_text(encoding="utf-8"))
                    return self._players_cache
                except json.JSONDecodeError:
                    log.warning("Player cache was corrupt; refetching")
        log.info("Fetching full NFL player list from Sleeper (cached for %.0fh)", max_age_hours)
        players = await self._get("/v1/players/nfl")
        if not isinstance(players, dict):
            raise SleeperAPIError("Unexpected players payload")
        cache_file.write_text(json.dumps(players), encoding="utf-8")
        self._players_cache = players
        return players

    # ------------------------------------------------------------------ projections / stats
    # These two feeds are used by the Sleeper web app; they are not in the documented API
    # but require no authentication. Callers should tolerate an empty result.

    async def get_projections(
        self, season: str, week: int, positions: list[str] | None = None, season_type: str = "regular"
    ) -> list[dict]:
        params: list[tuple[str, str]] = [("season_type", season_type), ("order_by", "ppr")]
        for pos in positions or FANTASY_POSITIONS:
            params.append(("position[]", pos))
        try:
            data = await self._get(f"/projections/nfl/{season}/{week}", params=params)
        except SleeperAPIError as exc:
            log.warning("Projections unavailable: %s", exc)
            return []
        return data if isinstance(data, list) else []

    async def get_season_projections(
        self, season: str, positions: list[str] | None = None, season_type: str = "regular"
    ) -> list[dict]:
        """Full-season projections (one row per player, stats summed over the season)."""
        params: list[tuple[str, str]] = [("season_type", season_type), ("order_by", "ppr")]
        for pos in positions or FANTASY_POSITIONS:
            params.append(("position[]", pos))
        try:
            data = await self._get(f"/projections/nfl/{season}", params=params)
        except SleeperAPIError as exc:
            log.warning("Season projections unavailable: %s", exc)
            return []
        return data if isinstance(data, list) else []

    async def get_stats(
        self, season: str, week: int, positions: list[str] | None = None, season_type: str = "regular"
    ) -> list[dict]:
        params: list[tuple[str, str]] = [("season_type", season_type), ("order_by", "pts_ppr")]
        for pos in positions or FANTASY_POSITIONS:
            params.append(("position[]", pos))
        try:
            data = await self._get(f"/stats/nfl/{season}/{week}", params=params)
        except SleeperAPIError as exc:
            log.warning("Stats unavailable: %s", exc)
            return []
        return data if isinstance(data, list) else []
