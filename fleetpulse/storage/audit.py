"""Tamper-evident audit log: each record stores sha256(prev_hash || canonical record).
Verification walks the chain: O(n). Rows are insert-only (UPDATE/DELETE revoked from app role)."""
from __future__ import annotations

import hashlib
import json
import threading

GENESIS = "0" * 64
_lock = threading.Lock()


def record_hash(prev: str, actor: str, fleet_id, action: str, resource: str, detail: dict) -> str:
    body = json.dumps([prev, actor, fleet_id, action, resource, detail], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def append(conn, actor: str, fleet_id, action: str, resource: str, detail: dict | None = None) -> str:
    detail = detail or {}
    with _lock, conn.transaction():
        # advisory lock serialises chain appends across API replicas
        conn.execute("SELECT pg_advisory_xact_lock(42)")
        row = conn.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        prev = row[0] if row else GENESIS
        h = record_hash(prev, actor, fleet_id, action, resource, detail)
        conn.execute("INSERT INTO audit_log (actor, fleet_id, action, resource, detail, prev_hash, hash) "
                     "VALUES (%s,%s,%s,%s,%s,%s,%s)", (actor, fleet_id, action, resource, json.dumps(detail), prev, h))
    return h


def verify(conn) -> dict:
    prev, n = GENESIS, 0
    for id_, actor, fleet_id, action, resource, detail, p, h in conn.execute(
            "SELECT id, actor, fleet_id, action, resource, detail, prev_hash, hash FROM audit_log ORDER BY id"):
        if p != prev or record_hash(p, actor, fleet_id, action, resource, detail) != h:
            return {"ok": False, "broken_at": id_, "checked": n}
        prev, n = h, n + 1
    return {"ok": True, "checked": n}


class AuditWriter:
    """Group-commit audit writer for high-volume *read* audit events: records are
    queued in memory and chained + inserted in one transaction every `interval_s`.
    Mutations (erase / approve / work orders / agent actions) use synchronous `append`.
    Trade-off: a crash can lose <= interval_s of read-access records (documented in ADR-005)."""

    def __init__(self, pool, interval_s: float = 0.1):
        import queue
        self.pool, self.interval = pool, interval_s
        self.q: queue.SimpleQueue = queue.SimpleQueue()
        self._stop = threading.Event()
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def submit(self, actor, fleet_id, action, resource, detail=None):
        self.q.put((actor, fleet_id, action, resource, detail or {}))

    def flush(self) -> int:
        items = []
        while not self.q.empty():
            items.append(self.q.get_nowait())
        if not items:
            return 0
        try:
            self._write(items)
        except Exception:
            for it in items:          # at-least-once: put back and retry on next tick
                self.q.put(it)
            raise
        return len(items)

    def _write(self, items):
        with _lock, self.pool.connection() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(42)")
            row = conn.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
            prev = row[0] if row else GENESIS
            rows = []
            for actor, fleet_id, action, resource, detail in items:
                h = record_hash(prev, actor, fleet_id, action, resource, detail)
                rows.append((actor, fleet_id, action, resource, json.dumps(detail), prev, h))
                prev = h
            with conn.cursor() as cur:
                cur.executemany("INSERT INTO audit_log (actor, fleet_id, action, resource, detail, prev_hash, hash) "
                                "VALUES (%s,%s,%s,%s,%s,%s,%s)", rows)

    def _run(self):
        while not self._stop.wait(self.interval):
            try:
                self.flush()
            except Exception:  # noqa: BLE001 - retried next tick; items already dequeued are logged
                import logging
                logging.getLogger("fleetpulse.audit").exception("audit flush failed")

    def close(self):
        self._stop.set()
        self.flush()
