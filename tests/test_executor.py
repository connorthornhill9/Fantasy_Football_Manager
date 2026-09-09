import asyncio

from ffm.executor import ExecTarget, Executor
from ffm.proposals import Proposal
from ffm.sleeper.auth import SleeperAuthError

TARGET = ExecTarget(league_id="L1", roster_id=4, leg=7)


class FakeAuth:
    def __init__(self, fail: bool = False):
        self.calls: list[tuple] = []
        self.fail = fail

    async def _record(self, name, *args, **kwargs):
        if self.fail:
            raise SleeperAuthError("player is on waivers")
        self.calls.append((name, args, kwargs))
        return {"transaction_id": "tx1", "status": "complete"}

    async def add_drop(self, league_id, roster_id, adds, drops):
        return await self._record("add_drop", league_id, roster_id, adds=adds, drops=drops)

    async def waiver_claim(self, league_id, roster_id, adds, drops, faab_bid=None):
        return await self._record("waiver_claim", league_id, roster_id, adds=adds, drops=drops, faab_bid=faab_bid)

    async def set_starters(self, league_id, roster_id, starters, week):
        return await self._record("set_starters", league_id, roster_id, starters=starters, week=week)

    async def set_reserve(self, league_id, roster_id, reserve):
        return await self._record("set_reserve", league_id, roster_id, reserve=reserve)

    async def set_taxi(self, league_id, roster_id, taxi):
        return await self._record("set_taxi", league_id, roster_id, taxi=taxi)

    async def propose_trade(self, league_id, adds, drops, draft_picks=None, waiver_budget=None, expires_at=None):
        return await self._record("propose_trade", league_id, adds=adds, drops=drops)


class FakePublic:
    """Read client returning a fixed roster: players 7 and 9 active, 8 on IR, 5 on taxi."""

    def __init__(self):
        self.roster = {"roster_id": 4, "players": ["5", "7", "8", "9"], "starters": ["7"], "reserve": ["8"], "taxi": ["5"]}

    async def get_rosters(self, league_id):
        return [dict(self.roster), {"roster_id": 9, "players": ["A"]}]


def run(coro):
    return asyncio.run(coro)


def test_add_drop_maps_to_create_free_agent():
    auth = FakeAuth()
    p = Proposal(kind="add_drop", adds=["100"], drops=["200"], rationale="r")
    result = run(Executor(auth).execute(p, TARGET))
    assert result.ok and not result.dry_run
    assert auth.calls == [("add_drop", ("L1", 4), {"adds": ["100"], "drops": ["200"]})]
    assert "tx1" in result.message


def test_waiver_claim_passes_bid():
    auth = FakeAuth()
    p = Proposal(kind="waiver_claim", adds=["100"], drops=[], faab_bid=17, rationale="r")
    run(Executor(auth).execute(p, TARGET))
    assert auth.calls[0][0] == "waiver_claim"
    assert auth.calls[0][2]["faab_bid"] == 17


def test_lineup_sets_starters_for_the_week():
    auth = FakeAuth()
    p = Proposal(kind="lineup", starters=["1", "2", "0"], rationale="r")
    run(Executor(auth).execute(p, TARGET))
    assert auth.calls[0] == ("set_starters", ("L1", 4), {"starters": ["1", "2", "0"], "week": 7})


def test_lineup_verification_reads_the_matchup_leg():
    class LegAuth(FakeAuth):
        async def get_matchup_leg(self, league_id, roster_id, week):
            return {"roster_id": roster_id, "round": week, "starters": ["1", "2", "0"]}

    p = Proposal(kind="lineup", starters=["1", "2", "0"], rationale="r")
    result = run(Executor(LegAuth(), public=FakePublic(), verify_delay=0).execute(p, TARGET))
    assert result.ok and "Verified" in result.message


def test_ir_taxi_activate_send_full_lists():
    auth = FakeAuth()
    ex = Executor(auth, public=FakePublic(), verify_delay=0)
    # verification reads the same fixed roster, so it reports a mismatch; we only check the calls here
    run(ex.execute(Proposal(kind="ir", drops=["9"], rationale="r"), TARGET))
    run(ex.execute(Proposal(kind="taxi", drops=["7"], rationale="r"), TARGET))
    run(ex.execute(Proposal(kind="activate_ir", adds=["8"], rationale="r"), TARGET))
    assert auth.calls[0] == ("set_reserve", ("L1", 4), {"reserve": ["8", "9"]})
    assert auth.calls[1] == ("set_taxi", ("L1", 4), {"taxi": ["5", "7"]})
    assert auth.calls[2] == ("set_reserve", ("L1", 4), {"reserve": []})


def test_ir_without_read_client_is_an_error():
    result = run(Executor(FakeAuth()).execute(Proposal(kind="ir", drops=["9"], rationale="r"), TARGET))
    assert not result.ok and "read client" in result.message


def test_verification_reports_missing_change():
    auth = FakeAuth()
    ex = Executor(auth, public=FakePublic(), verify_delay=0)
    result = run(ex.execute(Proposal(kind="add_drop", adds=["Z"], drops=["7"], rationale="r"), TARGET))
    assert not result.ok and "does not show it" in result.message
    ok = run(ex.execute(Proposal(kind="add", adds=["9"], rationale="r"), TARGET))
    assert ok.ok and "Verified" in ok.message


def test_trade_maps_receivers_and_senders():
    auth = FakeAuth()
    p = Proposal(kind="trade", trade_partner_roster_id=9, i_give=["A"], i_get=["B", "C"], rationale="r")
    run(Executor(auth).execute(p, TARGET))
    _, _, kwargs = auth.calls[0]
    assert kwargs["adds"] == [("B", 4), ("C", 4), ("A", 9)]
    assert kwargs["drops"] == [("B", 9), ("C", 9), ("A", 4)]


def test_dry_run_without_auth():
    p = Proposal(kind="add", adds=["100"], rationale="r")
    result = run(Executor(None).execute(p, TARGET))
    assert result.ok and result.dry_run and "DRY RUN" in result.message


def test_dry_run_flag_overrides_auth():
    auth = FakeAuth()
    p = Proposal(kind="add", adds=["100"], rationale="r")
    result = run(Executor(auth, dry_run=True).execute(p, TARGET))
    assert result.dry_run and auth.calls == []


def test_sleeper_error_is_reported_not_raised():
    p = Proposal(kind="drop", drops=["100"], rationale="r")
    result = run(Executor(FakeAuth(fail=True)).execute(p, TARGET))
    assert not result.ok and "waivers" in result.message


class WaiverAwareAuth(FakeAuth):
    """Rejects direct adds for player 'W' (on waivers) and claims for player 'F' (a free agent)."""

    async def add_drop(self, league_id, roster_id, adds, drops):
        if "W" in adds:
            raise SleeperAuthError("player is currently on waivers")
        return await super().add_drop(league_id, roster_id, adds, drops)

    async def waiver_claim(self, league_id, roster_id, adds, drops, faab_bid=0):
        if "F" in adds:
            raise SleeperAuthError("player is a free agent and not on waivers")
        return await super().waiver_claim(league_id, roster_id, adds, drops, faab_bid)


def test_add_falls_back_to_waiver_claim_when_allowed():
    auth = WaiverAwareAuth()
    p = Proposal(kind="add_drop", adds=["W"], drops=["2"], rationale="r")
    result = run(Executor(auth, auto_claim=True).execute(p, TARGET))
    assert result.ok and "waiver claim instead" in result.message
    assert auth.calls[-1][0] == "waiver_claim" and auth.calls[-1][2]["drops"] == ["2"]


def test_add_on_waivers_is_reported_when_auto_claim_is_off():
    auth = WaiverAwareAuth()
    p = Proposal(kind="add_drop", adds=["W"], drops=["2"], rationale="r")
    result = run(Executor(auth, auto_claim=False).execute(p, TARGET))
    assert not result.ok and "still on waivers" in result.message
    assert auth.calls == []


def test_claim_falls_back_to_direct_add():
    auth = WaiverAwareAuth()
    p = Proposal(kind="waiver_claim", adds=["F"], drops=[], faab_bid=0, rationale="r")
    result = run(Executor(auth).execute(p, TARGET))
    assert result.ok and "Added directly" in result.message
    assert auth.calls[-1][0] == "add_drop"
