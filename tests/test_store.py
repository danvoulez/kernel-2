from pathlib import Path

import pytest
import sqlite3

from engine import Conflict, GENESIS_SCHEMA, IntegrityError, NotFound, Store, ValidationError
from engine.model import canonical_json, content_hash


@pytest.fixture
def store(tmp_path: Path):
    with Store(tmp_path / "engine.db") as value:
        yield value


def template(store: Store, target: str = "programa"):
    return store.create_genesis_template(
        {
            "alvo": target,
            "nome": f"{target}_v1",
            "json_schema": {
                "type": "object",
                "required": ["nome"],
                "additionalProperties": False,
                "properties": {"nome": {"type": "string", "minLength": 1}},
            },
        },
        at="2026-09-20T00:00:00Z",
    )


def test_canonical_hash_is_stable_and_timestamp_is_not_identity(store: Store):
    schema = template(store)
    first = store.create("programa", schema.hash, {"nome": "Olá"}, at="2026-09-20T01:00:00Z")
    second = store.create("programa", schema.hash, {"nome": "Olá"}, at="2030-01-01T00:00:00Z")
    assert first.hash == second.hash
    assert second.criado_em == "2026-09-20T01:00:00Z"
    assert canonical_json({"b": 1, "a": "á"}) == b'{"a":"\xc3\xa1","b":1}'
    assert first.hash == content_hash(tipo="programa", schema=schema.hash, pais=(), corpo={"nome": "Olá"})


def test_validation_and_genesis_boundary(store: Store):
    schema = template(store)
    with pytest.raises(ValidationError, match="required"):
        store.create("programa", schema.hash, {})
    with pytest.raises(ValidationError, match="genesis"):
        store.create("programa", GENESIS_SCHEMA, {"nome": "escape"})
    with pytest.raises(ValidationError, match="targets"):
        store.create("tarefa", schema.hash, {"nome": "wrong"})


def test_graph_events_and_pointer_compare_and_swap(store: Store):
    schema = template(store)
    parent = store.create("programa", schema.hash, {"nome": "one"})
    child = store.create("programa", schema.hash, {"nome": "two"}, [parent.hash])
    store.set_pointer("producao/codigo", parent.hash)
    store.set_pointer("producao/codigo", child.hash, expected=parent.hash)

    assert store.resolve_pointer("producao/codigo").hash == child.hash
    assert [row["hash"] for row in store.lineage(child.hash)] == [child.hash, parent.hash]
    assert [row["hash"] for row in store.lineage(parent.hash, direction="forward")] == [parent.hash, child.hash]
    assert store.events(child.hash)[-1]["dados"]["de"] == parent.hash
    with pytest.raises(Conflict):
        store.set_pointer("producao/codigo", parent.hash, expected="f" * 64)


def test_transaction_rolls_back_missing_parent(store: Store):
    schema = template(store)
    with pytest.raises(NotFound):
        store.create("programa", schema.hash, {"nome": "orphan"}, ["a" * 64])
    assert store.db.execute("SELECT count(*) FROM objetos").fetchone()[0] == 1


def test_blob_round_trip_and_tamper_detection(store: Store):
    digest = store.put_blob(b"weights")
    assert store.get_blob(digest) == b"weights"
    (store.blobs / digest).write_bytes(b"tampered")
    with pytest.raises(IntegrityError):
        store.get_blob(digest)


def test_database_tamper_is_detected(store: Store):
    schema = template(store)
    obj = store.create("programa", schema.hash, {"nome": "safe"})
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        store.db.execute("UPDATE objetos SET corpo_json = ? WHERE hash = ?", ('{"nome":"unsafe"}', obj.hash))


def test_full_integrity_audit(store: Store):
    schema = template(store)
    obj = store.create("programa", schema.hash, {"nome": "audited"})
    store.set_pointer("programas/ativo", obj.hash)
    assert store.audit() == {"objetos": 2, "arestas": 0, "eventos": 3, "ponteiros": 1}
