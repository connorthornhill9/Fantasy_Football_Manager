"""Authenticated Sleeper GraphQL client used to make roster moves.

Sleeper's documented API is read-only. Roster changes go through the same GraphQL
endpoint the sleeper.com web app uses. The mutations below were confirmed against the
live schema via GraphQL introspection (September 2026); they are unofficial and may
change without notice. The token is the `authorization` header value the web app
sends, captured once from the browser's DevTools.
"""
from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

GRAPHQL_URL = "https://sleeper.com/graphql"
TRANSACTION_FIELDS = "transaction_id status type created leg adds drops roster_ids settings metadata"
ROSTER_FIELDS = "roster_id league_id players starters reserve taxi"


class SleeperAuthError(RuntimeError):
    pass


@dataclass
class TokenInfo:
    user_id: str
    display_name: str
    issued_at: int
    expires_at: int  # 0 when the token carries no expiry claim

    @property
    def is_expired(self) -> bool:
        return self.expires_at > 0 and time.time() >= self.expires_at

    @property
    def seconds_remaining(self) -> int | None:
        if self.expires_at <= 0:
            return None
        return max(0, int(self.expires_at - time.time()))


def _decode_jwt_payload(token: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        raise SleeperAuthError("SLEEPER_TOKEN does not look like a JWT (expected three dot-separated parts)")
    body = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(body.encode()))
    except Exception as exc:  # noqa: BLE001
        raise SleeperAuthError(f"Could not decode token payload: {exc}") from exc


def inspect_token(token: str) -> TokenInfo:
    """Parse (without verifying) a Sleeper JWT to learn whose it is and when it expires."""
    payload = _decode_jwt_payload(token)
    return TokenInfo(
        user_id=str(payload.get("user_id", "")),
        display_name=str(payload.get("display_name", "")),
        issued_at=int(payload.get("iat", 0) or 0),
        expires_at=int(payload.get("exp", 0) or 0),
    )


def _normalize_errors(errors: Any) -> list[dict]:
    if isinstance(errors, dict):
        return [errors]
    if isinstance(errors, list):
        return [e if isinstance(e, dict) else {"message": str(e)} for e in errors]
    return [{"message": str(errors)}]


class SleeperAuth:
    """Async GraphQL client for authenticated Sleeper operations."""

    def __init__(self, token: str, timeout: float = 30.0):
        if not token:
            raise SleeperAuthError("No Sleeper token provided")
        self.token = token
        self.token_info = inspect_token(token)
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self, op_name: str) -> dict[str, str]:
        return {
            "authorization": self.token,
            "content-type": "application/json",
            "accept": "application/json",
            "origin": "https://sleeper.com",
            "referer": "https://sleeper.com/",
            "x-sleeper-graphql-op": op_name,
            "user-agent": "fantasy-football-manager/0.1",
        }

    async def gql(self, op_name: str, query: str, variables: dict | None = None) -> dict:
        if self.token_info.is_expired:
            raise SleeperAuthError(
                "Sleeper token has expired. Capture a fresh one from sleeper.com and update SLEEPER_TOKEN."
            )
        try:
            resp = await self._client.post(
                GRAPHQL_URL,
                headers=self._headers(op_name),
                json={"operationName": op_name, "query": query, "variables": variables or {}},
            )
        except httpx.HTTPError as exc:
            raise SleeperAuthError(f"Network error talking to Sleeper: {exc}") from exc
        try:
            body = resp.json()
        except ValueError as exc:
            raise SleeperAuthError(
                f"Non-JSON response from Sleeper (HTTP {resp.status_code}): {resp.text[:200]}"
            ) from exc
        if body.get("errors"):
            errors = _normalize_errors(body["errors"])
            codes = {str(e.get("code", "")).lower() for e in errors}
            messages = "; ".join(str(e.get("message") or e) for e in errors)
            if "unauthorized" in codes or resp.status_code in (401, 403):
                raise SleeperAuthError(f"Sleeper rejected the token (unauthorized): {messages}")
            raise SleeperAuthError(f"Sleeper returned an error: {messages}")
        return body.get("data") or {}

    # ------------------------------------------------------------------ free agents / waivers

    async def add_drop(self, league_id: str, roster_id: int, adds: list[str], drops: list[str]) -> dict:
        """Free-agent add and/or drop in one transaction (league_create_transaction, type free_agent)."""
        if not adds and not drops:
            raise ValueError("add_drop needs at least one add or one drop")
        query = f"""
        mutation league_create_transaction($league_id: Snowflake!, $type: String!, $k_adds: [String], $v_adds: [Int], $k_drops: [String], $v_drops: [Int]) {{
          league_create_transaction(league_id: $league_id, type: $type, k_adds: $k_adds, v_adds: $v_adds, k_drops: $k_drops, v_drops: $v_drops) {{
            {TRANSACTION_FIELDS}
          }}
        }}
        """
        data = await self.gql(
            "league_create_transaction",
            query,
            {
                "league_id": league_id,
                "type": "free_agent",
                "k_adds": list(adds),
                "v_adds": [roster_id] * len(adds),
                "k_drops": list(drops),
                "v_drops": [roster_id] * len(drops),
            },
        )
        return data.get("league_create_transaction") or {}

    async def waiver_claim(
        self, league_id: str, roster_id: int, adds: list[str], drops: list[str], faab_bid: int | None = None
    ) -> dict:
        """Submit a waiver claim. Pass faab_bid only in FAAB leagues."""
        if not adds:
            raise ValueError("waiver_claim needs at least one add")
        query = f"""
        mutation submit_waiver_claim($league_id: Snowflake!, $k_adds: [String], $v_adds: [Int], $k_drops: [String], $v_drops: [Int], $k_settings: [String], $v_settings: [Int]) {{
          submit_waiver_claim(league_id: $league_id, k_adds: $k_adds, v_adds: $v_adds, k_drops: $k_drops, v_drops: $v_drops, k_settings: $k_settings, v_settings: $v_settings) {{
            {TRANSACTION_FIELDS}
          }}
        }}
        """
        variables: dict = {
            "league_id": league_id,
            "k_adds": list(adds),
            "v_adds": [roster_id] * len(adds),
            "k_drops": list(drops),
            "v_drops": [roster_id] * len(drops),
            "k_settings": [],
            "v_settings": [],
        }
        if faab_bid is not None:
            variables["k_settings"] = ["waiver_bid"]
            variables["v_settings"] = [int(faab_bid)]
        data = await self.gql("submit_waiver_claim", query, variables)
        return data.get("submit_waiver_claim") or {}

    async def cancel_waiver_claim(self, league_id: str, transaction_id: str, leg: int) -> dict:
        query = f"""
        mutation cancel_waiver_claim($league_id: Snowflake!, $transaction_id: Snowflake!, $leg: Int!) {{
          cancel_waiver_claim(league_id: $league_id, transaction_id: $transaction_id, leg: $leg) {{ {TRANSACTION_FIELDS} }}
        }}
        """
        data = await self.gql(
            "cancel_waiver_claim", query, {"league_id": league_id, "transaction_id": transaction_id, "leg": leg}
        )
        return data.get("cancel_waiver_claim") or {}

    # ------------------------------------------------------------------ lineup / roster slots

    async def set_starters(self, league_id: str, roster_id: int, starters: list[str], week: int) -> dict:
        """Set the starters for a week. Order must match the league's roster_positions (excluding BN/IR/TAXI).

        In-season lineups live on the week's matchup leg (that is what sleeper.com shows), so this updates
        the leg for `week` and then the roster's default starters for later weeks.
        """
        leg_query = """
        mutation update_matchup_leg($league_id: Snowflake!, $roster_id: Int!, $round: Int!, $leg: Int!, $starters: [String]) {
          update_matchup_leg(league_id: $league_id, roster_id: $roster_id, round: $round, leg: $leg, starters: $starters) {
            round leg roster_id starters
          }
        }
        """
        data = await self.gql(
            "update_matchup_leg",
            leg_query,
            {"league_id": league_id, "roster_id": roster_id, "round": week, "leg": week, "starters": starters},
        )
        result = data.get("update_matchup_leg") or {}
        default_query = f"""
        mutation roster_update_starters($league_id: Snowflake!, $roster_id: Int!, $starters: [String]) {{
          roster_update_starters(league_id: $league_id, roster_id: $roster_id, starters: $starters) {{ {ROSTER_FIELDS} }}
        }}
        """
        try:
            await self.gql("roster_update_starters", default_query, {"league_id": league_id, "roster_id": roster_id, "starters": starters})
        except SleeperAuthError as exc:  # the week is set; the default is a courtesy
            log.warning("roster_update_starters failed after update_matchup_leg succeeded: %s", exc)
        return result

    async def get_matchup_leg(self, league_id: str, roster_id: int, week: int) -> dict | None:
        """The roster's matchup leg for a week: live starters, players and points."""
        query = """
        query matchup_legs_related_to_roster($league_id: Snowflake!, $roster_id: Int!, $s: Int!, $e: Int!) {
          matchup_legs_related_to_roster(league_id: $league_id, roster_id: $roster_id, start_round: $s, end_round: $e) {
            round leg roster_id matchup_id starters players points proj_points
          }
        }
        """
        data = await self.gql(
            "matchup_legs_related_to_roster",
            query,
            {"league_id": league_id, "roster_id": roster_id, "s": week, "e": week},
        )
        legs = data.get("matchup_legs_related_to_roster") or []
        return next((leg for leg in legs if leg.get("roster_id") == roster_id and leg.get("round") == week), None)

    async def set_reserve(self, league_id: str, roster_id: int, reserve: list[str]) -> dict:
        """Replace the full injured-reserve list."""
        query = f"""
        mutation roster_update_reserve($league_id: Snowflake!, $roster_id: Int!, $reserve: [String]) {{
          roster_update_reserve(league_id: $league_id, roster_id: $roster_id, reserve: $reserve) {{ {ROSTER_FIELDS} }}
        }}
        """
        data = await self.gql(
            "roster_update_reserve", query, {"league_id": league_id, "roster_id": roster_id, "reserve": reserve}
        )
        return data.get("roster_update_reserve") or {}

    async def set_taxi(self, league_id: str, roster_id: int, taxi: list[str]) -> dict:
        """Replace the full taxi-squad list."""
        query = f"""
        mutation roster_update_taxi($league_id: Snowflake!, $roster_id: Int!, $taxi: [String]) {{
          roster_update_taxi(league_id: $league_id, roster_id: $roster_id, taxi: $taxi) {{ {ROSTER_FIELDS} }}
        }}
        """
        data = await self.gql("roster_update_taxi", query, {"league_id": league_id, "roster_id": roster_id, "taxi": taxi})
        return data.get("roster_update_taxi") or {}

    # ------------------------------------------------------------------ trades

    async def propose_trade(
        self,
        league_id: str,
        adds: list[tuple[str, int]],
        drops: list[tuple[str, int]],
        draft_picks: list[str] | None = None,
        waiver_budget: list[str] | None = None,
        expires_at: int | None = None,
    ) -> dict:
        """Propose a trade.

        adds:  (player_id, receiving_roster_id) pairs, i.e. who GETS each player
        drops: (player_id, sending_roster_id) pairs, i.e. who SENDS each player
        """
        query = f"""
        mutation propose_trade(
          $league_id: Snowflake!, $k_adds: [String], $v_adds: [Int], $k_drops: [String], $v_drops: [Int],
          $draft_picks: [String], $waiver_budget: [String], $expires_at: Int
        ) {{
          propose_trade(
            league_id: $league_id, k_adds: $k_adds, v_adds: $v_adds, k_drops: $k_drops, v_drops: $v_drops,
            draft_picks: $draft_picks, waiver_budget: $waiver_budget, expires_at: $expires_at
          ) {{ {TRANSACTION_FIELDS} }}
        }}
        """
        data = await self.gql(
            "propose_trade",
            query,
            {
                "league_id": league_id,
                "k_adds": [p for p, _ in adds],
                "v_adds": [r for _, r in adds],
                "k_drops": [p for p, _ in drops],
                "v_drops": [r for _, r in drops],
                "draft_picks": draft_picks or [],
                "waiver_budget": waiver_budget or [],
                "expires_at": expires_at,
            },
        )
        return data.get("propose_trade") or {}

    async def cancel_trade(self, league_id: str, transaction_id: str, leg: int) -> dict:
        query = f"""
        mutation cancel_trade($league_id: Snowflake!, $transaction_id: Snowflake!, $leg: Int!) {{
          cancel_trade(league_id: $league_id, transaction_id: $transaction_id, leg: $leg) {{ {TRANSACTION_FIELDS} }}
        }}
        """
        data = await self.gql("cancel_trade", query, {"league_id": league_id, "transaction_id": transaction_id, "leg": leg})
        return data.get("cancel_trade") or {}

    async def get_league_rosters(self, league_id: str) -> list[dict]:
        """Fresh roster state straight from Sleeper (the public REST API can lag by a minute or more)."""
        query = f"""
        query league_rosters($league_id: Snowflake!) {{
          league_rosters(league_id: $league_id) {{ {ROSTER_FIELDS} owner_id co_owners settings metadata }}
        }}
        """
        data = await self.gql("league_rosters", query, {"league_id": league_id})
        return data.get("league_rosters") or []

    async def get_pending_trades(self, league_id: str, limit: int = 100) -> list[dict]:
        """All trades in 'proposed' status (the public API only exposes completed ones)."""
        return await self.get_transactions(league_id, types=["trade"], statuses=["proposed"], limit=limit)

    async def get_transactions(
        self, league_id: str, types: list[str] | None = None, statuses: list[str] | None = None, limit: int = 100
    ) -> list[dict]:
        """League transactions with arbitrary type/status filters (e.g. my pending waiver claims)."""
        query = """
        query league_transactions_filtered($league_id: Snowflake!, $type_filters: [String], $status_filters: [String], $limit: Int) {
          league_transactions_filtered(league_id: $league_id, type_filters: $type_filters, status_filters: $status_filters, limit: $limit) {
            transaction_id status type creator consenter_ids roster_ids created status_updated leg adds drops metadata settings draft_picks waiver_budget
          }
        }
        """
        data = await self.gql(
            "league_transactions_filtered",
            query,
            {"league_id": league_id, "type_filters": types, "status_filters": statuses, "limit": limit},
        )
        return data.get("league_transactions_filtered") or []
