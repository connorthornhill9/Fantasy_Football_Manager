from ffm.proposals import Proposal
from ffm.store import Store


def make_store(tmp_path):
    return Store(tmp_path / "test.sqlite3")


def test_proposal_roundtrip(tmp_path):
    store = make_store(tmp_path)
    run_id = store.create_run("weekly_waivers")
    p = Proposal(kind="waiver_claim", adds=["1"], drops=["2"], faab_bid=12, rationale="why", confidence="high")
    rec = store.add_proposal(p, run_id)
    assert rec.status == "pending"
    loaded = store.get_proposal(rec.id)
    assert loaded is not None
    assert loaded.proposal == p
    assert [r.id for r in store.pending()] == [rec.id]
    store.set_discord_message(rec.id, 111, 222)
    loaded = store.get_proposal(rec.id)
    assert (loaded.discord_channel_id, loaded.discord_message_id) == (111, 222)


def test_claim_is_atomic(tmp_path):
    store = make_store(tmp_path)
    rec = store.add_proposal(Proposal(kind="drop", drops=["1"], rationale="r"), None)
    assert store.claim_for_execution(rec.id)
    assert not store.claim_for_execution(rec.id)
    store.set_status(rec.id, "executed", {"ok": True, "message": "done"})
    loaded = store.get_proposal(rec.id)
    assert loaded.status == "executed"
    assert loaded.result["message"] == "done"
    assert loaded.executed_at is not None
    assert store.pending() == []


def test_reject_and_expire(tmp_path):
    store = make_store(tmp_path)
    a = store.add_proposal(Proposal(kind="drop", drops=["1"], rationale="r"), None)
    b = store.add_proposal(Proposal(kind="drop", drops=["2"], rationale="r"), None)
    store.set_status(a.id, "rejected")
    assert store.get_proposal(a.id).decided_at is not None
    assert store.expire_pending_older_than("9999-01-01T00:00:00+00:00") == 1
    assert store.get_proposal(b.id).status == "expired"


def test_runs(tmp_path):
    store = make_store(tmp_path)
    run_id = store.create_run("lineup", focus="x")
    store.finish_run(run_id, "done", "summary", {"input_tokens": 5})
    run = store.get_run(run_id)
    assert run["status"] == "done" and run["summary"] == "summary"
    assert store.recent_runs(1)[0]["id"] == run_id
