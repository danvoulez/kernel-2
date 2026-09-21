from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from engine import Conflict, NotFound, Sessions, Store


class Clock:
    def __init__(self):
        self.value = datetime(2026, 9, 20, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


@pytest.fixture
def system(tmp_path: Path):
    clock = Clock()
    with Store(tmp_path / "engine.db") as store:
        schema = store.create_genesis_template(
            {"alvo": "card", "json_schema": {"type": "object", "required": ["missao"], "properties": {"missao": {"type": "string"}}}}
        )
        card = store.create("card", schema.hash, {"missao": "medir"})
        sessions = Sessions(store, clock)
        sessions.register_slot("premium-h", "test", "secret", 60, ["metrics", "session_end"])
        yield store, sessions, clock, card


def test_claim_auth_completion_and_idempotent_replay(system):
    store, sessions, _, card = system
    sessions.schedule(card.hash, "premium-h", priority=9)
    with pytest.raises(NotFound):
        sessions.begin("premium-h", "wrong")
    contract = sessions.begin("premium-h", "secret")
    assert contract and contract.card_hash == card.hash
    assert contract.tools == ("metrics", "session_end")
    output = {"nota": 8}
    assert sessions.end(card.hash, contract.lease_id, output, idempotency_key="request-1") == output
    assert sessions.end(card.hash, contract.lease_id, output, idempotency_key="request-1") == output
    with pytest.raises(Conflict, match="different output"):
        sessions.end(card.hash, contract.lease_id, {"nota": 9}, idempotency_key="request-1")
    assert [event["tipo"] for event in store.events(card.hash)] == ["objeto_criado", "agendado", "reivindicado", "concluido"]


def test_expired_lease_is_durably_rejected(system):
    store, sessions, clock, card = system
    sessions.schedule(card.hash, "premium-h")
    contract = sessions.begin("premium-h", "secret")
    clock.value += timedelta(seconds=61)
    with pytest.raises(Conflict, match="expired"):
        sessions.end(card.hash, contract.lease_id, {}, idempotency_key="late")
    state = store.db.execute("SELECT state FROM card_queue WHERE card_hash=?", (card.hash,)).fetchone()[0]
    assert state == "expired"
    assert store.events(card.hash)[-1]["tipo"] == "expirado"


def test_slot_definition_and_single_schedule_are_immutable(system):
    _, sessions, _, card = system
    with pytest.raises(Conflict, match="immutable"):
        sessions.register_slot("premium-h", "other", "secret", 60, ["metrics"])
    sessions.schedule(card.hash, "premium-h")
    with pytest.raises(Conflict, match="already scheduled"):
        sessions.schedule(card.hash, "premium-h")
