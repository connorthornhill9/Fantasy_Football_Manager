"""Turn an approved proposal into Sleeper API calls."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass

from .proposals import Proposal
from .sleeper.auth import SleeperAuth, SleeperAuthError
from .sleeper.public import SleeperAPIError, SleeperPublic

log = logging.getLogger(__name__)


@dataclass
class ExecTarget:
    league_id: str
    roster_id: int
    leg: int | None = None


@dataclass
class ExecutionResult:
    ok: bool
    message: str
    raw: dict | None = None
    dry_run: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class Executor:
    def __init__(
        self, auth: SleeperAuth | None, dry_run: bool = False, public: SleeperPublic | None = None, verify_delay: float = 4.0
    ):
        self.auth = auth
        self.dry_run = dry_run or auth is None
        self.public = public
        self.verify_delay = verify_delay

    async def execute(self, proposal: Proposal, target: ExecTarget) -> ExecutionResult:
        plan = self.describe_call(proposal, target)
        if self.dry_run:
            log.info("DRY RUN: %s", plan)
            return ExecutionResult(ok=True, message=f"[DRY RUN] would call {plan}", dry_run=True)
        assert self.auth is not None
        executed = proposal
        note = ""
        try:
            try:
                raw = await self._dispatch(proposal, target)
            except SleeperAuthError as exc:
                alternate = self._alternate(proposal, exc)
                if alternate is None:
                    raise
                # Sleeper decides whether a player is a free agent or on waivers; retry the other way.
                log.info("Sleeper said %r; retrying as %s", str(exc), alternate.kind)
                executed = alternate
                plan = self.describe_call(alternate, target)
                raw = await self._dispatch(alternate, target)
                note = (
                    " Submitted as a waiver claim instead (the player is on waivers; it processes at the next waiver run)."
                    if alternate.kind == "waiver_claim"
                    else " Added directly instead (the player was a free agent, not on waivers)."
                )
        except SleeperAuthError as exc:
            log.warning("Sleeper rejected %s: %s", plan, exc)
            return ExecutionResult(ok=False, message=str(exc))
        except ValueError as exc:
            return ExecutionResult(ok=False, message=f"Invalid proposal: {exc}")
        status = (raw or {}).get("status")
        tx = (raw or {}).get("transaction_id")
        detail = f" (status: {status}, transaction {tx})" if status or tx else ""
        problem = await self.verify(executed, target)
        if problem:
            log.warning("Post-check after %s: %s", plan, problem)
            return ExecutionResult(ok=False, message=f"Sleeper accepted the request{detail} but the roster does not show it: {problem}", raw=raw)
        verified = " Verified on the roster." if self.public and self._verifiable(executed) else ""
        return ExecutionResult(ok=True, message=f"Done: {plan}{detail}.{note}{verified}", raw=raw)

    @staticmethod
    def _alternate(p: Proposal, exc: SleeperAuthError) -> Proposal | None:
        """If Sleeper rejected an add because the player is on waivers (or a claim because he is not), the other kind."""
        text = str(exc).lower()
        if p.kind in ("add", "add_drop") and p.adds and "waiver" in text:
            return p.model_copy(update={"kind": "waiver_claim", "faab_bid": p.faab_bid or 0})
        if p.kind == "waiver_claim" and ("free agent" in text or "not on waivers" in text):
            return p.model_copy(update={"kind": "add_drop" if p.drops else "add"})
        return None

    @staticmethod
    def _verifiable(p: Proposal) -> bool:
        return p.kind in ("add", "drop", "add_drop", "lineup", "ir", "activate_ir", "taxi")

    async def verify(self, p: Proposal, t: ExecTarget, attempts: int = 3) -> str | None:
        """Re-read the roster and confirm the change is visible. Returns a description of any mismatch."""
        if self.public is None or not self._verifiable(p):
            return None  # waiver claims and trades are pending by nature; nothing to check yet
        problem: str | None = "roster could not be read"
        for attempt in range(attempts):
            try:
                rosters = await self._fresh_rosters(t.league_id)
            except (SleeperAPIError, SleeperAuthError) as exc:
                problem = f"roster could not be read ({exc})"
            else:
                mine = next((r for r in rosters if int(r.get("roster_id", -1)) == t.roster_id), None)
                problem = self._check_roster(p, mine) if mine else "my roster was not found"
            if problem is None:
                return None
            if attempt < attempts - 1 and self.verify_delay > 0:
                await asyncio.sleep(self.verify_delay * (attempt + 1))
        return problem

    async def _fresh_rosters(self, league_id: str) -> list[dict]:
        """Authenticated GraphQL read when available (not cached); otherwise the public API."""
        getter = getattr(self.auth, "get_league_rosters", None)
        if getter is not None:
            return await getter(league_id)
        assert self.public is not None
        return await self.public.get_rosters(league_id)

    @staticmethod
    def _check_roster(p: Proposal, roster: dict) -> str | None:
        players = {str(x) for x in (roster.get("players") or [])}
        reserve = {str(x) for x in (roster.get("reserve") or [])}
        taxi = {str(x) for x in (roster.get("taxi") or [])}
        starters = [str(x) for x in (roster.get("starters") or [])]
        if p.kind in ("add", "drop", "add_drop"):
            missing = [x for x in p.adds if x not in players]
            lingering = [x for x in p.drops if x in players]
            if missing or lingering:
                return f"not yet on roster: {missing}; still on roster: {lingering}"
        elif p.kind == "lineup":
            if starters != [str(x) for x in (p.starters or [])]:
                return f"starters are {starters}"
        elif p.kind == "ir":
            if (p.drops or p.adds)[0] not in reserve:
                return "player is not on IR"
        elif p.kind == "activate_ir":
            if (p.adds or p.drops)[0] in reserve:
                return "player is still on IR"
        elif p.kind == "taxi":
            if (p.drops or p.adds)[0] not in taxi:
                return "player is not on the taxi squad"
        return None

    def describe_call(self, p: Proposal, t: ExecTarget) -> str:
        if p.kind in ("add", "drop", "add_drop"):
            return f"add/drop adds={p.adds} drops={p.drops}"
        if p.kind == "waiver_claim":
            bid = f" bid=${p.faab_bid}" if p.faab_bid is not None else ""
            return f"waiver claim adds={p.adds} drops={p.drops}{bid}"
        if p.kind == "lineup":
            return f"set starters week {t.leg}: {p.starters}"
        if p.kind == "ir":
            return f"move to IR player={(p.drops or p.adds)[0]}"
        if p.kind == "activate_ir":
            return f"activate from IR player={(p.adds or p.drops)[0]}"
        if p.kind == "taxi":
            return f"move to taxi player={(p.drops or p.adds)[0]}"
        if p.kind == "trade":
            return f"propose_trade partner={p.trade_partner_roster_id} give={p.i_give} get={p.i_get}"
        return p.kind

    async def _my_roster(self, t: ExecTarget) -> dict:
        if self.public is None and getattr(self.auth, "get_league_rosters", None) is None:
            raise ValueError("a Sleeper read client is required for IR/taxi moves")
        rosters = await self._fresh_rosters(t.league_id)
        mine = next((r for r in rosters if int(r.get("roster_id", -1)) == t.roster_id), None)
        if mine is None:
            raise ValueError("my roster was not found")
        return mine

    async def _dispatch(self, p: Proposal, t: ExecTarget) -> dict:
        auth = self.auth
        assert auth is not None
        if p.kind in ("add", "drop", "add_drop"):
            return await auth.add_drop(t.league_id, t.roster_id, adds=list(p.adds), drops=list(p.drops))
        if p.kind == "waiver_claim":
            return await auth.waiver_claim(
                t.league_id, t.roster_id, adds=list(p.adds), drops=list(p.drops), faab_bid=p.faab_bid
            )
        if p.kind == "lineup":
            if not p.starters:
                raise ValueError("lineup proposal has no starters")
            return await auth.set_starters(t.league_id, t.roster_id, [str(s) for s in p.starters])
        if p.kind in ("ir", "activate_ir"):
            roster = await self._my_roster(t)
            reserve = [str(x) for x in (roster.get("reserve") or [])]
            pid = (p.drops or p.adds)[0] if p.kind == "ir" else (p.adds or p.drops)[0]
            reserve = [x for x in reserve if x != pid] + ([pid] if p.kind == "ir" else [])
            return await auth.set_reserve(t.league_id, t.roster_id, reserve)
        if p.kind == "taxi":
            roster = await self._my_roster(t)
            pid = (p.drops or p.adds)[0]
            taxi = [str(x) for x in (roster.get("taxi") or []) if str(x) != pid] + [pid]
            return await auth.set_taxi(t.league_id, t.roster_id, taxi)
        if p.kind == "trade":
            if p.trade_partner_roster_id is None:
                raise ValueError("trade proposal has no partner")
            partner = int(p.trade_partner_roster_id)
            adds = [(pid, t.roster_id) for pid in p.i_get] + [(pid, partner) for pid in p.i_give]
            drops = [(pid, partner) for pid in p.i_get] + [(pid, t.roster_id) for pid in p.i_give]
            return await auth.propose_trade(t.league_id, adds=adds, drops=drops)
        raise ValueError(f"unsupported proposal kind {p.kind}")
