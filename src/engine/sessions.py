from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import secrets
import sqlite3
from typing import Any, Callable

from .errors import Conflict, NotFound, ValidationError
from .model import canonical_json
from .store import Store


_SESSION_DDL = """
CREATE TABLE IF NOT EXISTS slots (
  id TEXT PRIMARY KEY,
  platform TEXT NOT NULL,
  token_hash TEXT NOT NULL CHECK(length(token_hash) = 64),
  duration_seconds INTEGER NOT NULL CHECK(duration_seconds BETWEEN 1 AND 86400),
  tools_json TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1))
) STRICT;
CREATE TABLE IF NOT EXISTS card_queue (
  card_hash TEXT PRIMARY KEY REFERENCES objetos(hash),
  slot_id TEXT NOT NULL REFERENCES slots(id),
  state TEXT NOT NULL CHECK(state IN ('scheduled','claimed','completed','expired')),
  priority INTEGER NOT NULL,
  available_at TEXT NOT NULL,
  lease_id TEXT,
  lease_until TEXT,
  claimed_at TEXT,
  completed_at TEXT,
  output_json TEXT
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS one_live_lease ON card_queue(lease_id) WHERE lease_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS idempotency (
  card_hash TEXT NOT NULL REFERENCES objetos(hash),
  operation TEXT NOT NULL,
  key TEXT NOT NULL,
  result_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(card_hash, operation, key)
) STRICT;
"""


@dataclass(frozen=True, slots=True)
class Contract:
    card_hash: str
    lease_id: str
    lease_until: str
    tools: tuple[str, ...]
    body: dict[str, Any]


class Sessions:
    """Deterministic card scheduler and lease boundary for ephemeral intelligence."""

    def __init__(self, store: Store, clock: Callable[[], datetime] | None = None) -> None:
        self.store = store
        self.db = store.db
        self.db.executescript(_SESSION_DDL)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def register_slot(self, slot_id: str, platform: str, token: str, duration_seconds: int, tools: list[str]) -> None:
        if not slot_id or not platform or not token:
            raise ValidationError("slot id, platform and token are required")
        if not 1 <= duration_seconds <= 86400:
            raise ValidationError("slot duration must be between 1 and 86400 seconds")
        if not tools or any(not isinstance(tool, str) or not tool for tool in tools):
            raise ValidationError("a slot needs named tools")
        digest = sha256(token.encode()).hexdigest()
        payload = canonical_json(sorted(set(tools))).decode()
        with self.store.transaction():
            current = self.db.execute("SELECT platform, token_hash, duration_seconds, tools_json FROM slots WHERE id=?", (slot_id,)).fetchone()
            values = (platform, digest, duration_seconds, payload)
            if current and tuple(current) != values:
                raise Conflict("slot definitions are immutable; disable and create a new id")
            self.db.execute("INSERT OR IGNORE INTO slots(id,platform,token_hash,duration_seconds,tools_json) VALUES(?,?,?,?,?)", (slot_id, *values))

    def schedule(self, card_hash: str, slot_id: str, *, priority: int = 0, available_at: str | None = None) -> None:
        card = self.store.get(card_hash)
        if card.tipo != "card":
            raise ValidationError("only card objects can be scheduled")
        when = available_at or self._iso(self.clock())
        with self.store.transaction():
            if not self.db.execute("SELECT 1 FROM slots WHERE id=? AND enabled=1", (slot_id,)).fetchone():
                raise NotFound(f"enabled slot not found: {slot_id}")
            try:
                self.db.execute("INSERT INTO card_queue(card_hash,slot_id,state,priority,available_at) VALUES(?,?,'scheduled',?,?)", (card_hash, slot_id, priority, when))
            except sqlite3.IntegrityError as exc:
                raise Conflict("card was already scheduled") from exc
            self.store._event(card_hash, "agendado", {"slot": slot_id, "prioridade": priority}, when)

    def begin(self, slot_id: str, token: str) -> Contract | None:
        now = self.clock()
        now_text = self._iso(now)
        with self.store.transaction():
            slot = self.db.execute("SELECT * FROM slots WHERE id=? AND enabled=1", (slot_id,)).fetchone()
            if not slot or not secrets.compare_digest(slot["token_hash"], sha256(token.encode()).hexdigest()):
                raise NotFound("slot not found")  # Deliberately does not reveal auth failures.
            self._expire(now_text)
            card = self.db.execute(
                "SELECT * FROM card_queue WHERE slot_id=? AND state='scheduled' AND available_at<=? ORDER BY priority DESC, available_at, card_hash LIMIT 1",
                (slot_id, now_text),
            ).fetchone()
            if not card:
                return None
            lease_id = secrets.token_hex(32)
            until = self._iso(now + timedelta(seconds=slot["duration_seconds"]))
            changed = self.db.execute(
                "UPDATE card_queue SET state='claimed',lease_id=?,lease_until=?,claimed_at=? WHERE card_hash=? AND state='scheduled'",
                (lease_id, until, now_text, card["card_hash"]),
            ).rowcount
            if changed != 1:
                raise Conflict("card claim race")
            self.store._event(card["card_hash"], "reivindicado", {"slot": slot_id, "lease_until": until}, now_text)
            obj = self.store.get(card["card_hash"])
            return Contract(obj.hash, lease_id, until, tuple(json.loads(slot["tools_json"])), obj.corpo)

    def end(self, card_hash: str, lease_id: str, output: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        if not idempotency_key:
            raise ValidationError("idempotency key is required")
        canonical = canonical_json(output).decode()
        now = self._iso(self.clock())
        expired = False
        with self.store.transaction():
            previous = self.db.execute("SELECT result_json FROM idempotency WHERE card_hash=? AND operation='session_end' AND key=?", (card_hash, idempotency_key)).fetchone()
            if previous:
                result = json.loads(previous[0])
                if result != output:
                    raise Conflict("idempotency key was reused with different output")
                return result
            row = self.db.execute("SELECT * FROM card_queue WHERE card_hash=?", (card_hash,)).fetchone()
            if not row or row["state"] != "claimed" or not secrets.compare_digest(row["lease_id"] or "", lease_id):
                raise Conflict("no matching live lease")
            if row["lease_until"] < now:
                self.db.execute("UPDATE card_queue SET state='expired' WHERE card_hash=?", (card_hash,))
                self.store._event(card_hash, "expirado", {"motivo": "lease_timeout"}, now)
                expired = True
            else:
                self.db.execute("UPDATE card_queue SET state='completed',completed_at=?,output_json=? WHERE card_hash=?", (now, canonical, card_hash))
                self.db.execute("INSERT INTO idempotency VALUES(?, 'session_end', ?, ?, ?)", (card_hash, idempotency_key, canonical, now))
                self.store._event(card_hash, "concluido", {"idempotency_key": idempotency_key}, now)
        if expired:
            raise Conflict("lease expired")
        return output

    def _expire(self, now: str) -> int:
        rows = list(self.db.execute("SELECT card_hash FROM card_queue WHERE state='claimed' AND lease_until < ?", (now,)))
        for row in rows:
            self.db.execute("UPDATE card_queue SET state='expired' WHERE card_hash=?", (row[0],))
            self.store._event(row[0], "expirado", {"motivo": "lease_timeout"}, now)
        return len(rows)

    @staticmethod
    def _iso(value: datetime) -> str:
        if value.tzinfo is None:
            raise ValidationError("clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
