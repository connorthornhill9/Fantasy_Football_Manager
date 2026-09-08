"""Authenticated Sleeper GraphQL client used to make roster moves.

Sleeper's documented API is read-only. Roster changes go through the same GraphQL
endpoint the sleeper.com web app uses. The mutation shapes below were reverse
engineered by the community (see github.com/cameron-eth/sleeper-sdk) and may change
without notice. The token is the `authorization` header value the web app sends,
captured once from the browser's DevTools.
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
        """Free-agent add and/or drop in one transaction."""
        if not adds and not drops:
            raise ValueError("add_drop needs at least one add or one drop")
        query = """
        mutation create_free_agent($league_id: Snowflake!, $roster_id: Int!, $adds: JSON, $drops: JSON) {
          create_free_agent(league_id: $league_id, roster_id: $roster_id, adds: $adds, drops: $drops) {
            transaction_id status type created adds drops
          }
        }
        """
        data = await self.gql(
            "create_free_agent",
            query,
            {
                "league_id": league_id,
                "roster_id": roster_id,
                "adds": {pid: roster_id for pid in adds},
                "drops": {pid: roster_id for pid in drops},
            },
        )
        return data.get("create_free_agent") or {}

    async def waiver_claim(
        self, league_id: str, roster_id: int, adds: list[str], drops: list[str], faab_bid: int = 0
    ) -> dict:
        if not adds:
            raise ValueError("waiver_claim needs at least one add")
        query = """
        mutation create_waiver_claim($league_id: Snowflake!, $roster_id: Int!, $adds: JSON, $drops: JSON, $waiver_budget: Int) {
          create_waiver_claim(league_id: $league_id, roster_id: $roster_id, adds: $adds, drops: $drops, waiver_budget: $waiver_budget) {
            transaction_id status type created adds drops settings
          }
        }
        """
        data = await self.gql(
            "create_waiver_claim",
            query,
            {
                "league_id": league_id,
                "roster_id": roster_id,
                "adds": {pid: roster_id for pid in adds},
                "drops": {pid: roster_id for pid in drops},
                "waiver_budget": int(faab_bid or 0),
            },
        )
        return data.get("create_waiver_claim") or {}

    async def cancel_waiver_claim(self, league_id: str, transaction_id: str, leg: int) -> dict:
        query = """
        mutation cancel_waiver_claim($league_id: Snowflake!, $transaction_id: Snowflake!, $leg: Int!) {
          cancel_waiver_claim(league_id: $league_id, transaction_id: $transaction_id, leg: $leg) {
            transaction_id status type created
          }
        }
        """
        data = await self.gql(
            "cancel_waiver_claim",
            query,
            {"league_id": league_id, "transaction_id": transaction_id, "leg": leg},
        )
        return data.get("cancel_waiver_claim") or {}

    # ------------------------------------------------------------------ lineup / roster slots

    async def set_starters(self, league_id: str, roster_id: int, starters: list[str], leg: int | None) -> dict:
        """Set the starters list. Order must match the league's roster_positions (excluding BN/IR/TAXI)."""
        query = """
        mutation update_roster_starters($league_id: Snowflake!, $roster_id: Int!, $starters: [String]!, $leg: Int) {
          update_roster_starters(league_id: $league_id, roster_id: $roster_id, starters: $starters, leg: $leg) {
            roster_id starters players reserve taxi
          }
        }
        """
        data = await self.gql(
            "update_roster_starters",
            query,
            {"league_id": league_id, "roster_id": roster_id, "starters": starters, "leg": leg},
        )
        return data.get("update_roster_starters") or {}

    async def move_to_ir(self, league_id: str, roster_id: int, player_id: str) -> dict:
        query = """
        mutation move_to_ir($league_id: Snowflake!, $roster_id: Int!, $player_id: String!) {
          move_to_ir(league_id: $league_id, roster_id: $roster_id, player_id: $player_id) { roster_id reserve }
        }
        """
        data = await self.gql(
            "move_to_ir", query, {"league_id": league_id, "roster_id": roster_id, "player_id": player_id}
        )
        return data.get("move_to_ir") or {}

    async def activate_from_ir(self, league_id: str, roster_id: int, player_id: str) -> dict:
        query = """
        mutation activate_from_ir($league_id: Snowflake!, $roster_id: Int!, $player_id: String!) {
          activate_from_ir(league_id: $league_id, roster_id: $roster_id, player_id: $player_id) { roster_id reserve players }
        }
        """
        data = await self.gql(
            "activate_from_ir", query, {"league_id": league_id, "roster_id": roster_id, "player_id": player_id}
        )
        return data.get("activate_from_ir") or {}

    async def move_to_taxi(self, league_id: str, roster_id: int, player_id: str) -> dict:
        query = """
        mutation move_to_taxi($league_id: Snowflake!, $roster_id: Int!, $player_id: String!) {
          move_to_taxi(league_id: $league_id, roster_id: $roster_id, player_id: $player_id) { roster_id taxi }
        }
        """
        data = await self.gql(
            "move_to_taxi", query, {"league_id": league_id, "roster_id": roster_id, "player_id": player_id}
        )
        return data.get("move_to_taxi") or {}

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
        query = """
        mutation propose_trade(
          $league_id: Snowflake!, $k_adds: [String], $v_adds: [Int], $k_drops: [String], $v_drops: [Int],
          $draft_picks: [String], $waiver_budget: [String], $expires_at: Int
        ) {
          propose_trade(
            league_id: $league_id, k_adds: $k_adds, v_adds: $v_adds, k_drops: $k_drops, v_drops: $v_drops,
            draft_picks: $draft_picks, waiver_budget: $waiver_budget, expires_at: $expires_at
          ) { transaction_id status type created leg metadata settings }
        }
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
        query = """
        mutation cancel_trade($league_id: Snowflake!, $transaction_id: Snowflake!, $leg: Int!) {
          cancel_trade(league_id: $league_id, transaction_id: $transaction_id, leg: $leg) { transaction_id status type created }
        }
        """
        data = await self.gql(
            "cancel_trade", query, {"league_id": league_id, "transaction_id": transaction_id, "leg": leg}
        )
        return data.get("cancel_trade") or {}

    async def get_pending_trades(self, league_id: str, limit: int = 100) -> list[dict]:
        """All trades in 'proposed' status (the public API only exposes completed ones)."""
        query = """
        query league_transactions_filtered($league_id: Snowflake!, $type_filters: [String], $status_filters: [String], $limit: Int) {
          league_transactions_filtered(league_id: $league_id, type_filters: $type_filters, status_filters: $status_filters, limit: $limit) {
            transaction_id status type creator consenter_ids roster_ids created leg adds drops metadata settings draft_picks waiver_budget
          }
        }
        """
        data = await self.gql(
            "league_transactions_filtered",
            query,
            {"league_id": league_id, "type_filters": ["trade"], "status_filters": ["proposed"], "limit": limit},
        )
        return data.get("league_transactions_filtered") or []
