"""SQLite persistence for analysis runs and proposals."""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .proposals import Proposal

STATUSES = ("pending", "approved", "executing", "executed", "failed", "rejected", "expired", "advice")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ProposalRecord:
    id: int
    run_id: int | None
    created_at: str
    proposal: Proposal
    status: str
    discord_channel_id: int | None = None
    discord_message_id: int | None = None
    result: dict | None = None
    decided_at: str | None = None
    executed_at: str | None = None

    @property
    def kind(self) -> str:
        return self.proposal.kind


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    def _migrate(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    focus TEXT,
                    status TEXT NOT NULL DEFAULT 'running',
                    summary TEXT,
                    usage TEXT,
                    finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS proposals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER,
                    created_at TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    discord_channel_id INTEGER,
                    discord_message_id INTEGER,
                    result TEXT,
                    decided_at TEXT,
                    executed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status);
                CREATE TABLE IF NOT EXISTS locks (
                    player_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outcomes (
                    proposal_id INTEGER NOT NULL,
                    eval_week INTEGER NOT NULL,
                    gain REAL,
                    side_a REAL,
                    side_b REAL,
                    verdict TEXT NOT NULL,
                    detail TEXT,
                    computed_at TEXT NOT NULL,
                    PRIMARY KEY (proposal_id, eval_week)
                );
                CREATE TABLE IF NOT EXISTS projection_log (
                    week INTEGER NOT NULL,
                    player_id TEXT NOT NULL,
                    position TEXT,
                    sleeper_proj REAL,
                    espn_proj REAL,
                    actual REAL,
                    PRIMARY KEY (week, player_id)
                );
                CREATE TABLE IF NOT EXISTS week_predictions (
                    week INTEGER PRIMARY KEY,
                    win_prob REAL,
                    my_points REAL,
                    opp_points REAL,
                    won INTEGER,
                    lineup_points REAL,
                    best_points REAL
                );
                CREATE TABLE IF NOT EXISTS reports (
                    week INTEGER PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    text TEXT NOT NULL
                );
                """
            )

    # ------------------------------------------------------------------ evaluation data

    def upsert_projection_log(self, rows: list[tuple[int, str, str | None, float | None, float | None]]) -> None:
        with self._lock, self._conn:
            self._conn.executemany(
                """INSERT INTO projection_log (week, player_id, position, sleeper_proj, espn_proj)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(week, player_id) DO UPDATE SET position=excluded.position,
                       sleeper_proj=excluded.sleeper_proj, espn_proj=excluded.espn_proj""",
                rows,
            )

    def set_projection_actuals(self, week: int, actuals: dict[str, float]) -> None:
        with self._lock, self._conn:
            self._conn.executemany(
                "UPDATE projection_log SET actual=? WHERE week=? AND player_id=?",
                [(pts, week, pid) for pid, pts in actuals.items()],
            )

    def projection_log(self, up_to_week: int | None = None) -> list[dict]:
        with self._lock:
            if up_to_week is None:
                rows = self._conn.execute("SELECT * FROM projection_log WHERE actual IS NOT NULL").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM projection_log WHERE actual IS NOT NULL AND week<=?", (up_to_week,)
                ).fetchall()
        return [dict(r) for r in rows]

    def upsert_prediction(self, week: int, win_prob: float | None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO week_predictions (week, win_prob) VALUES (?, ?) ON CONFLICT(week) DO UPDATE SET win_prob=excluded.win_prob",
                (week, win_prob),
            )

    def set_week_result(self, week: int, my_points: float, opp_points: float | None, lineup_points: float, best_points: float) -> None:
        won = None if opp_points is None else int(my_points > opp_points)
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO week_predictions (week, my_points, opp_points, won, lineup_points, best_points)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(week) DO UPDATE SET my_points=excluded.my_points, opp_points=excluded.opp_points,
                       won=excluded.won, lineup_points=excluded.lineup_points, best_points=excluded.best_points""",
                (week, my_points, opp_points, won, lineup_points, best_points),
            )

    def week_predictions(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM week_predictions ORDER BY week").fetchall()
        return [dict(r) for r in rows]

    def upsert_outcome(self, proposal_id: int, eval_week: int, gain: float | None, side_a: float | None, side_b: float | None, verdict: str, detail: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO outcomes (proposal_id, eval_week, gain, side_a, side_b, verdict, detail, computed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(proposal_id, eval_week) DO UPDATE SET gain=excluded.gain, side_a=excluded.side_a,
                       side_b=excluded.side_b, verdict=excluded.verdict, detail=excluded.detail, computed_at=excluded.computed_at""",
                (proposal_id, eval_week, gain, side_a, side_b, verdict, detail, _now()),
            )

    def outcomes(self, eval_week: int | None = None) -> list[dict]:
        with self._lock:
            if eval_week is None:
                rows = self._conn.execute("SELECT * FROM outcomes ORDER BY eval_week, proposal_id").fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM outcomes WHERE eval_week=? ORDER BY proposal_id", (eval_week,)).fetchall()
        return [dict(r) for r in rows]

    def save_report(self, week: int, text: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO reports (week, created_at, text) VALUES (?, ?, ?) ON CONFLICT(week) DO UPDATE SET created_at=excluded.created_at, text=excluded.text",
                (week, _now(), text),
            )

    def latest_report(self) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM reports ORDER BY week DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def all_proposals(self) -> list[ProposalRecord]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM proposals ORDER BY id").fetchall()
        return [self._row_to_record(r) for r in rows]

    # ------------------------------------------------------------------ locks

    def add_lock(self, player_id: str, name: str) -> bool:
        """Protect a player from being dropped or traded away. Returns False if already locked."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO locks (player_id, name, created_at) VALUES (?, ?, ?)",
                (str(player_id), name, _now()),
            )
            return cur.rowcount == 1

    def remove_lock(self, player_id: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM locks WHERE player_id=?", (str(player_id),))
            return cur.rowcount == 1

    def locks(self) -> dict[str, str]:
        """player_id -> name for every locked player."""
        with self._lock:
            rows = self._conn.execute("SELECT player_id, name FROM locks ORDER BY name").fetchall()
        return {r["player_id"]: r["name"] for r in rows}

    # ------------------------------------------------------------------ runs

    def create_run(self, trigger: str, focus: str | None = None) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO runs (created_at, trigger, focus) VALUES (?, ?, ?)", (_now(), trigger, focus)
            )
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, summary: str | None, usage: dict | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE runs SET status=?, summary=?, usage=?, finished_at=? WHERE id=?",
                (status, summary, json.dumps(usage) if usage else None, _now(), run_id),
            )

    def get_run(self, run_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def recent_runs(self, limit: int = 5) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ proposals

    def add_proposal(self, proposal: Proposal, run_id: int | None, status: str = "pending") -> ProposalRecord:
        if status not in STATUSES:
            raise ValueError(f"Unknown status {status}")
        created = _now()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO proposals (run_id, created_at, kind, payload, status) VALUES (?, ?, ?, ?, ?)",
                (run_id, created, proposal.kind, proposal.model_dump_json(), status),
            )
            pid = int(cur.lastrowid)
        return ProposalRecord(id=pid, run_id=run_id, created_at=created, proposal=proposal, status=status)

    def _row_to_record(self, row: sqlite3.Row) -> ProposalRecord:
        return ProposalRecord(
            id=int(row["id"]),
            run_id=row["run_id"],
            created_at=row["created_at"],
            proposal=Proposal.model_validate_json(row["payload"]),
            status=row["status"],
            discord_channel_id=row["discord_channel_id"],
            discord_message_id=row["discord_message_id"],
            result=json.loads(row["result"]) if row["result"] else None,
            decided_at=row["decided_at"],
            executed_at=row["executed_at"],
        )

    def get_proposal(self, proposal_id: int) -> ProposalRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        return self._row_to_record(row) if row else None

    def list_proposals(
        self, status: str | None = None, limit: int = 50, run_id: int | None = None
    ) -> list[ProposalRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if run_id is not None:
            clauses.append("run_id=?")
            params.append(run_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM proposals {where} ORDER BY id DESC LIMIT ?", (*params, limit)
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def pending(self) -> list[ProposalRecord]:
        return list(reversed(self.list_proposals(status="pending", limit=200)))

    def set_discord_message(self, proposal_id: int, channel_id: int, message_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE proposals SET discord_channel_id=?, discord_message_id=? WHERE id=?",
                (channel_id, message_id, proposal_id),
            )

    def set_status(self, proposal_id: int, status: str, result: dict | None = None) -> None:
        if status not in STATUSES:
            raise ValueError(f"Unknown status {status}")
        now = _now()
        with self._lock, self._conn:
            self._conn.execute(
                """UPDATE proposals SET status=?,
                        result=COALESCE(?, result),
                        decided_at=CASE WHEN ? IN ('approved','rejected') THEN ? ELSE decided_at END,
                        executed_at=CASE WHEN ? IN ('executed','failed') THEN ? ELSE executed_at END
                   WHERE id=?""",
                (status, json.dumps(result) if result is not None else None, status, now, status, now, proposal_id),
            )

    def claim_for_execution(self, proposal_id: int) -> bool:
        """Atomically move a pending/approved proposal to 'executing'. Returns False if it was already taken."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE proposals SET status='executing', decided_at=COALESCE(decided_at, ?) "
                "WHERE id=? AND status IN ('pending','approved')",
                (_now(), proposal_id),
            )
            return cur.rowcount == 1

    def expire_pending_older_than(self, cutoff_iso: str) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE proposals SET status='expired' WHERE status='pending' AND created_at < ?", (cutoff_iso,)
            )
            return cur.rowcount
