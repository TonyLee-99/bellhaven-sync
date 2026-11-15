"""SQLite state: runs, proposals, decisions and CRM writes.

Proposal lifecycle (every transition is a conditional UPDATE, so two tabs / two processes can't race):

    pending --approve--> applying --ok--> applied
       |                    |---error--> failed --retry--> applying
       |                    '---stale, nothing written--> superseded
       '--reject--> rejected
    pending --(no longer produced by the planner)--> superseded

`approved` is not a resting state: approving claims the proposal for writing in the same transaction.
A proposal's key hashes subject + current values + desired values (see plan.Proposal.compute_key), so
a rejected/applied key is never queued again, while a genuinely new situation gets a new key.
An applied key that the planner produces again means the CRM drifted back; it is re-queued ("regressed").
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, finished_at TEXT, stats TEXT);
CREATE TABLE IF NOT EXISTS proposals (
  key TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, payload TEXT NOT NULL,
  version TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', regressed INTEGER NOT NULL DEFAULT 0,
  first_seen_run INTEGER, last_seen_run INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, decision TEXT NOT NULL, reviewer TEXT,
  reason TEXT, version TEXT, decided_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS writes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, op_index INTEGER, method TEXT NOT NULL,
  account_id TEXT, request TEXT, response TEXT, ok INTEGER NOT NULL, at TEXT NOT NULL);
"""

IN_FLIGHT = ("applying", "failed")          # a write started and may be partly done: keep its records locked
REF_FIELDS = ("parent_id", "duplicate_of_account", "chow_current_account")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _version(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:12]


class Busy(RuntimeError):
    pass


class State:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    # ------------------------------------------------------------------ plumbing
    @contextmanager
    def tx(self):
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def q(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self._lock:
            return self.db.execute(sql, args).fetchall()

    @contextmanager
    def exclusive(self, what: str, wait: bool = True):
        """Single-flight lock shared by every process using this state DB (daily run, review app, CLI):
        a run never plans from a CRM snapshot taken while a write is in progress, and two writes never overlap."""
        lock_path = self.path.with_suffix(".lock")
        with open(lock_path, "w") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            except BlockingIOError as e:
                raise Busy(f"another {what} is in progress") from e
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    # ------------------------------------------------------------------ runs
    def start_run(self) -> int:
        with self.tx() as db:
            return db.execute("INSERT INTO runs(started_at) VALUES (?)", (now(),)).lastrowid

    def finish_run(self, run_id: int, stats: dict) -> None:
        with self.tx() as db:
            db.execute("UPDATE runs SET finished_at=?, stats=? WHERE id=?", (now(), json.dumps(stats, default=str), run_id))

    def last_run(self) -> dict | None:
        r = self.q("SELECT * FROM runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1")
        if not r:
            return None
        d = dict(r[0])
        d["stats"] = json.loads(d["stats"] or "{}")
        return d

    # ------------------------------------------------------------------ proposals
    def sync_proposals(self, run_id: int, proposals: list[dict], keep_scope: dict | None = None) -> dict:
        """Insert new proposals, refresh pending ones, re-queue regressed applied ones, and supersede pending
        proposals the planner no longer produces - except those whose scope this run didn't evaluate
        (locked records, or absence checks switched off by an incomplete crawl)."""
        keep_scope = keep_scope or {}
        keep_slugs, keep_ids = set(keep_scope.get("slugs", ())), set(keep_scope.get("ids", ()))
        keep_kinds_no_absence = keep_scope.get("absence_off", False)
        seen = {p["key"] for p in proposals}
        stats = {"new": 0, "still_pending": 0, "already_decided": 0, "regressed": 0, "superseded": 0, "kept_unevaluated": 0}
        with self.tx() as db:
            for p in proposals:
                ver = _version(p)
                row = db.execute("SELECT status, version FROM proposals WHERE key=?", (p["key"],)).fetchone()
                if row is None:
                    db.execute("INSERT INTO proposals(key,kind,title,payload,version,status,first_seen_run,last_seen_run,created_at,updated_at)"
                               " VALUES (?,?,?,?,?,'pending',?,?,?,?)",
                               (p["key"], p["kind"], p["title"], json.dumps(p), ver, run_id, run_id, now(), now()))
                    stats["new"] += 1
                elif row["status"] in ("pending", "superseded"):
                    if row["version"] != ver or row["status"] == "superseded":
                        db.execute("UPDATE proposals SET payload=?, title=?, version=?, status='pending', last_seen_run=?, updated_at=? WHERE key=?",
                                   (json.dumps(p), p["title"], ver, run_id, now(), p["key"]))
                    else:
                        db.execute("UPDATE proposals SET last_seen_run=? WHERE key=?", (run_id, p["key"]))
                    stats["still_pending"] += 1
                elif row["status"] == "applied":
                    db.execute("UPDATE proposals SET payload=?, version=?, status='pending', regressed=1, last_seen_run=?, updated_at=? WHERE key=?",
                               (json.dumps(p), ver, run_id, now(), p["key"]))
                    stats["regressed"] += 1
                else:
                    db.execute("UPDATE proposals SET last_seen_run=? WHERE key=?", (run_id, p["key"]))
                    stats["already_decided"] += 1
            for r in db.execute("SELECT key, payload FROM proposals WHERE status='pending'").fetchall():
                if r["key"] in seen:
                    continue
                p = json.loads(r["payload"])
                ids = {o.get("account_id") for o in p["ops"]} - {None}
                if (p.get("location_slug") in keep_slugs or ids & keep_ids or
                        (keep_kinds_no_absence and "not-on-site" in p.get("tags", []))):
                    stats["kept_unevaluated"] += 1
                    continue
                db.execute("UPDATE proposals SET status='superseded', updated_at=? WHERE key=?", (now(), r["key"]))
                stats["superseded"] += 1
        return stats

    def _row_to_dict(self, r) -> dict:
        d = json.loads(r["payload"])
        d.update(status=r["status"], version=r["version"], regressed=bool(r["regressed"]), updated_at=r["updated_at"])
        return d

    def list(self, status: str | None = None) -> list[dict]:
        if status:
            rows = self.q("SELECT * FROM proposals WHERE status=? ORDER BY created_at, key", (status,))
        else:
            rows = self.q("SELECT * FROM proposals ORDER BY created_at, key")
        return [self._row_to_dict(r) for r in rows]

    def get(self, key: str) -> dict | None:
        r = self.q("SELECT * FROM proposals WHERE key=?", (key,))
        if not r:
            return None
        d = self._row_to_dict(r[0])
        d["decisions"] = [dict(x) for x in self.q("SELECT * FROM decisions WHERE key=? ORDER BY id", (key,))]
        d["writes"] = [dict(x) for x in self.q("SELECT * FROM writes WHERE key=? ORDER BY id", (key,))]
        return d

    def counts(self) -> dict:
        return {r["status"]: r["n"] for r in self.q("SELECT status, COUNT(*) n FROM proposals GROUP BY status")}

    # ------------------------------------------------------------------ decisions (conditional transitions)
    def approve_and_claim(self, key: str, reviewer: str, reason: str, version: str | None) -> bool:
        """pending -> applying, only if the reviewer saw the current version. False if it changed or was taken."""
        with self.tx() as db:
            cur = db.execute("UPDATE proposals SET status='applying', updated_at=? WHERE key=? AND status='pending'"
                             + (" AND version=?" if version else ""),
                             (now(), key, *( [version] if version else [] )))
            if cur.rowcount != 1:
                return False
            db.execute("INSERT INTO decisions(key,decision,reviewer,reason,version,decided_at) VALUES (?,?,?,?,?,?)",
                       (key, "approved", reviewer, reason, version, now()))
            return True

    def reject(self, key: str, reviewer: str, reason: str, version: str | None) -> bool:
        with self.tx() as db:
            cur = db.execute("UPDATE proposals SET status='rejected', updated_at=? WHERE key=? AND status='pending'"
                             + (" AND version=?" if version else ""),
                             (now(), key, *( [version] if version else [] )))
            if cur.rowcount != 1:
                return False
            db.execute("INSERT INTO decisions(key,decision,reviewer,reason,version,decided_at) VALUES (?,?,?,?,?,?)",
                       (key, "rejected", reviewer, reason, version, now()))
            return True

    def claim_retry(self, key: str, reviewer: str) -> bool:
        with self.tx() as db:
            cur = db.execute("UPDATE proposals SET status='applying', updated_at=? WHERE key=? AND status='failed'", (now(), key))
            if cur.rowcount != 1:
                return False
            db.execute("INSERT INTO decisions(key,decision,reviewer,reason,decided_at) VALUES (?,?,?,?,?)",
                       (key, "retry", reviewer, "", now()))
            return True

    def close_failed(self, key: str, reviewer: str, reason: str) -> bool:
        """A person checked a failed proposal and decided it should not be retried (e.g. the account already
        exists): close it as superseded so its records are released and the next run re-plans from the CRM."""
        with self.tx() as db:
            cur = db.execute("UPDATE proposals SET status='superseded', updated_at=? WHERE key=? AND status='failed'", (now(), key))
            if cur.rowcount != 1:
                return False
            db.execute("INSERT INTO decisions(key,decision,reviewer,reason,decided_at) VALUES (?,?,?,?,?)",
                       (key, "closed", reviewer, reason, now()))
            return True

    def finish(self, key: str, status: str) -> None:
        assert status in ("applied", "failed", "superseded")
        with self.tx() as db:
            db.execute("UPDATE proposals SET status=?, updated_at=? WHERE key=? AND status='applying'", (status, now(), key))

    def recover_interrupted(self) -> int:
        """A crash mid-write leaves 'applying'; mark it failed so it can be retried (writes are resumable).
        Only call this while holding `exclusive()` - otherwise a live write could be mislabelled."""
        with self.tx() as db:
            return db.execute("UPDATE proposals SET status='failed', updated_at=? WHERE status='applying'", (now(),)).rowcount

    def in_flight_targets(self) -> tuple[set[str], set[str]]:
        ids, slugs = set(), set()
        for r in self.q(f"SELECT key, payload FROM proposals WHERE status IN ({','.join('?' * len(IN_FLIGHT))})", IN_FLIGHT):
            p = json.loads(r["payload"])
            if p.get("location_slug"):
                slugs.add(p["location_slug"])
            for op in p["ops"]:
                if op.get("account_id"):
                    ids.add(op["account_id"])
                for k in REF_FIELDS:
                    v = op["fields"].get(k)
                    if isinstance(v, str) and v and not v.startswith("$"):
                        ids.add(v)
                ids.update(req["account_id"] for req in op.get("requires", []))
            ids.update(w["account_id"] for w in self.q(
                "SELECT account_id FROM writes WHERE key=? AND method IN ('POST','REUSE') AND account_id IS NOT NULL", (r["key"],)))
        return ids - {None}, slugs

    # ------------------------------------------------------------------ write log
    def record_write(self, key: str, op_index: int | None, method: str, account_id: str | None, request: dict,
                     response, ok: bool) -> None:
        with self.tx() as db:
            db.execute("INSERT INTO writes(key,op_index,method,account_id,request,response,ok,at) VALUES (?,?,?,?,?,?,?,?)",
                       (key, op_index, method, account_id, json.dumps(request, default=str),
                        response if isinstance(response, str) else json.dumps(response, default=str), int(ok), now()))
