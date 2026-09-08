"""Player lookup helpers on top of Sleeper's player dictionary, including IDP positions."""
from __future__ import annotations

import re
from typing import Iterable

OFFENSE_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DEF"}
IDP_POSITIONS = {"DL", "LB", "DB"}
FANTASY_POSITIONS = OFFENSE_POSITIONS | IDP_POSITIONS

# Sleeper lists many defenders under a sub-position; map them to the fantasy position group.
POSITION_ALIASES = {
    "DE": "DL", "DT": "DL", "NT": "DL", "EDGE": "DL",
    "OLB": "LB", "ILB": "LB", "MLB": "LB",
    "CB": "DB", "S": "DB", "FS": "DB", "SS": "DB", "NB": "DB",
    "PK": "K", "DST": "DEF",
}

# Which fantasy positions may fill each lineup slot. Slots not listed accept only the identically named position.
SLOT_ELIGIBILITY: dict[str, set[str]] = {
    "FLEX": {"RB", "WR", "TE"},
    "SUPER_FLEX": {"QB", "RB", "WR", "TE"},
    "WRRB_FLEX": {"RB", "WR"},
    "REC_FLEX": {"WR", "TE"},
    "IDP_FLEX": {"DL", "LB", "DB"},
}
NON_STARTING_SLOTS = {"BN", "IR", "TAXI"}
EMPTY_SLOT = "0"

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def slot_positions(slot: str) -> set[str]:
    return SLOT_ELIGIBILITY.get(slot) or {slot}


def canonical_position(player: dict) -> str | None:
    pos = player.get("position")
    if pos in FANTASY_POSITIONS:
        return pos
    if pos in POSITION_ALIASES:
        return POSITION_ALIASES[pos]
    for fp in player.get("fantasy_positions") or []:
        if fp in FANTASY_POSITIONS:
            return fp
    return pos


def eligible_positions(player: dict) -> set[str]:
    """Every fantasy position group this player can fill (Sleeper allows some DL/LB dual eligibility)."""
    out: set[str] = set()
    canon = canonical_position(player)
    if canon in FANTASY_POSITIONS:
        out.add(canon)
    for fp in player.get("fantasy_positions") or []:
        fp = POSITION_ALIASES.get(fp, fp)
        if fp in FANTASY_POSITIONS:
            out.add(fp)
    return out


def slot_allows(slot: str, position: str | None, fantasy_positions: Iterable[str] | None = None) -> bool:
    positions = {POSITION_ALIASES.get(p, p) for p in (fantasy_positions or [])}
    if position:
        positions.add(POSITION_ALIASES.get(position, position))
    return bool(positions & slot_positions(slot))


def normalize_name(name: str) -> str:
    """Lowercase, strip punctuation and generational suffixes so ESPN and Sleeper names can be matched."""
    tokens = re.sub(r"[^a-z0-9 ]", "", name.lower().replace(".", "")).split()
    while tokens and tokens[-1] in _SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


class PlayerDB:
    def __init__(self, players: dict[str, dict]):
        self._players = players
        self._by_name: list[tuple[str, str]] | None = None

    def get(self, player_id: str) -> dict | None:
        return self._players.get(str(player_id))

    def __contains__(self, player_id: object) -> bool:
        return str(player_id) in self._players

    def name(self, player_id: str) -> str:
        p = self.get(player_id)
        if not p:
            return f"player {player_id}"
        if p.get("position") == "DEF":
            return f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or f"{player_id} D/ST"
        return (
            p.get("full_name")
            or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
            or f"player {player_id}"
        )

    def position(self, player_id: str) -> str | None:
        """Fantasy position group (DE -> DL, CB -> DB, ...)."""
        p = self.get(player_id)
        return canonical_position(p) if p else None

    def raw_position(self, player_id: str) -> str | None:
        p = self.get(player_id)
        return p.get("position") if p else None

    def team(self, player_id: str) -> str | None:
        p = self.get(player_id)
        return p.get("team") if p else None

    def label(self, player_id: str) -> str:
        """e.g. 'Bijan Robinson (RB, ATL)' or 'Maxx Crosby (DL/DE, LV)'."""
        p = self.get(player_id)
        if not p:
            return f"player {player_id}"
        canon = canonical_position(p) or "?"
        raw = p.get("position")
        pos = canon if (raw == canon or not raw) else f"{canon}/{raw}"
        team = p.get("team") or "FA"
        return f"{self.name(player_id)} ({pos}, {team})"

    def injury(self, player_id: str) -> str:
        p = self.get(player_id) or {}
        status = p.get("injury_status")
        if not status:
            return ""
        part = p.get("injury_body_part")
        return f"{status}" + (f" ({part})" if part else "")

    def is_fantasy_relevant(self, player: dict) -> bool:
        canon = canonical_position(player)
        if canon not in FANTASY_POSITIONS:
            return False
        if canon == "DEF":
            return True
        if not player.get("active", True):
            return False
        return bool(player.get("team"))

    def all_fantasy_relevant(self, positions: Iterable[str] | None = None) -> Iterable[tuple[str, dict]]:
        wanted = set(positions) if positions else None
        for pid, p in self._players.items():
            if not self.is_fantasy_relevant(p):
                continue
            if wanted is not None and not (eligible_positions(p) & wanted):
                continue
            yield pid, p

    def teammates(self, team: str, position: str) -> list[tuple[str, dict]]:
        """Players on an NFL team at a fantasy position, ordered by depth chart."""
        out = []
        for pid, p in self._players.items():
            if p.get("team") != team or not p.get("active", True):
                continue
            if position not in eligible_positions(p):
                continue
            out.append((pid, p))
        out.sort(key=lambda kv: (kv[1].get("depth_chart_order") is None, kv[1].get("depth_chart_order") or 99, kv[1].get("search_rank") or 9_999_999))
        return out

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """Case-insensitive name search; also matches an exact player id or team abbreviation for D/ST."""
        q = query.strip().lower()
        if not q:
            return []
        for key in (query.strip(), q.upper(), q):
            exact = self.get(key)
            if exact:
                return [dict(exact, player_id=key)]
        if self._by_name is None:
            self._by_name = [
                (normalize_name(self.name(pid)), pid)
                for pid, p in self._players.items()
                if canonical_position(p) in FANTASY_POSITIONS
            ]
        tokens = normalize_name(q).split()
        hits: list[tuple[int, str]] = []
        for name, pid in self._by_name:
            if all(t in name for t in tokens):
                p = self._players[pid]
                rank = 0 if (p.get("team") and p.get("active", True)) else 1
                hits.append((rank, pid))
        hits.sort()
        return [dict(self._players[pid], player_id=pid) for _, pid in hits[:limit]]
