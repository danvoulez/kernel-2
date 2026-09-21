from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Sequence

try:  # Optional acceleration; the kernel remains dependency-free.
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError
except ImportError:  # pragma: no cover - exercised in minimal deployments
    class SchemaError(Exception):
        pass

    class Draft202012Validator:
        def __init__(self, schema: dict[str, Any]):
            self.schema = schema

        @staticmethod
        def check_schema(schema: dict[str, Any]) -> None:
            if not isinstance(schema, dict) or schema.get("type") not in {None, "object"}:
                raise SchemaError("only object schemas are supported without jsonschema")

        def iter_errors(self, value: Any):
            schema = self.schema
            if schema.get("type") == "object" and not isinstance(value, dict):
                yield _SchemaError("must be object", ())
                return
            for key in schema.get("required", []):
                if key not in value:
                    yield _SchemaError(f"'{key}' is a required property", (key,))
            properties = schema.get("properties", {})
            if schema.get("additionalProperties") is False:
                for key in value:
                    if key not in properties:
                        yield _SchemaError(f"Additional properties are not allowed ('{key}' was unexpected)", (key,))
            for key, spec in properties.items():
                if key not in value:
                    continue
                val = value[key]
                expected = spec.get("type")
                if expected == "string" and (not isinstance(val, str) or len(val) < spec.get("minLength", 0)):
                    yield _SchemaError(f"{val!r} is not a valid string", (key,))

    class _SchemaError:
        def __init__(self, message: str, path: tuple[str, ...]):
            self.message, self.absolute_path, self.path = message, path, path

from .errors import Conflict, IntegrityError, NotFound, ValidationError
from .model import ObjectRecord, canonical_json, content_hash


GENESIS_SCHEMA = "0" * 64
OBJECT_TYPES = frozenset({"programa", "objetivo", "tarefa", "template", "card", "sessao", "artefato", "avaliacao", "decisao"})

_DDL = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS objetos (
  hash TEXT PRIMARY KEY CHECK(length(hash) = 64),
  tipo TEXT NOT NULL,
  schema_hash TEXT NOT NULL,
  pais_json TEXT NOT NULL,
  corpo_json TEXT NOT NULL,
  criado_em TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS arestas (
  de_hash TEXT NOT NULL REFERENCES objetos(hash),
  para_hash TEXT NOT NULL REFERENCES objetos(hash),
  relacao TEXT NOT NULL,
  PRIMARY KEY (de_hash, para_hash, relacao)
) STRICT;
CREATE TABLE IF NOT EXISTS eventos (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  objeto_hash TEXT NOT NULL REFERENCES objetos(hash),
  tipo TEXT NOT NULL,
  dados_json TEXT NOT NULL,
  em TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS ponteiros (
  nome TEXT PRIMARY KEY,
  hash TEXT NOT NULL REFERENCES objetos(hash),
  atualizado_em TEXT NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS idx_eventos_objeto_seq ON eventos(objeto_hash, seq);
CREATE INDEX IF NOT EXISTS idx_arestas_para ON arestas(para_hash);
CREATE TRIGGER IF NOT EXISTS objetos_no_update BEFORE UPDATE ON objetos BEGIN SELECT RAISE(ABORT, 'objetos are immutable'); END;
CREATE TRIGGER IF NOT EXISTS objetos_no_delete BEFORE DELETE ON objetos BEGIN SELECT RAISE(ABORT, 'objetos are immutable'); END;
CREATE TRIGGER IF NOT EXISTS eventos_no_update BEFORE UPDATE ON eventos BEGIN SELECT RAISE(ABORT, 'eventos are append-only'); END;
CREATE TRIGGER IF NOT EXISTS eventos_no_delete BEFORE DELETE ON eventos BEGIN SELECT RAISE(ABORT, 'eventos are append-only'); END;
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class Store:
    """Transactional source of truth. Every mutation emits an event atomically."""

    def __init__(self, database: str | Path, blobs: str | Path | None = None) -> None:
        self.database = str(database)
        self.blobs = Path(blobs) if blobs else Path(self.database).with_suffix(".blobs")
        self.blobs.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.database, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_DDL)

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def create(
        self,
        tipo: str,
        schema: str,
        corpo: dict[str, Any],
        pais: Sequence[str] = (),
        *,
        at: str | None = None,
    ) -> ObjectRecord:
        if tipo not in OBJECT_TYPES:
            raise ValidationError(f"unknown object type: {tipo}")
        if not _is_hash(schema):
            raise ValidationError("schema must be a lowercase sha256 hex digest")
        if not isinstance(corpo, dict):
            raise ValidationError("object body must be a JSON object")
        parents = tuple(pais)
        if len(set(parents)) != len(parents):
            raise ValidationError("duplicate parents are not allowed")
        canonical_json(corpo)
        with self.transaction():
            for parent in parents:
                if not _is_hash(parent):
                    raise ValidationError("parent must be a lowercase sha256 hex digest")
                self._require_hash(parent)
            self._validate(schema, tipo, corpo)
            digest = content_hash(tipo=tipo, schema=schema, pais=parents, corpo=corpo)
            existing = self._get_optional(digest)
            if existing:
                return existing
            timestamp = at or _now()
            self.db.execute(
                "INSERT INTO objetos VALUES (?, ?, ?, ?, ?, ?)",
                (digest, tipo, schema, canonical_json(parents).decode(), canonical_json(corpo).decode(), timestamp),
            )
            for parent in parents:
                self.db.execute("INSERT INTO arestas VALUES (?, ?, 'deriva_de')", (digest, parent))
            self._event(digest, "objeto_criado", {"tipo": tipo}, timestamp)
        return self.get(digest)

    def create_genesis_template(self, corpo: dict[str, Any], *, at: str | None = None) -> ObjectRecord:
        """Create a root template via the one explicit trust anchor.

        The schema regress cannot terminate in a content-addressed object. Therefore only
        templates can cite the all-zero built-in schema, and their JSON Schema is checked
        for validity before insertion. All ordinary objects must cite a stored template.
        """
        if "json_schema" not in corpo or "alvo" not in corpo:
            raise ValidationError("genesis template needs alvo and json_schema")
        try:
            Draft202012Validator.check_schema(corpo["json_schema"])
        except SchemaError as exc:
            raise ValidationError(f"invalid JSON Schema: {getattr(exc, 'message', str(exc))}") from exc
        return self.create("template", GENESIS_SCHEMA, corpo, at=at)

    def _validate(self, schema_hash: str, tipo: str, corpo: dict[str, Any]) -> None:
        if schema_hash == GENESIS_SCHEMA:
            if tipo != "template":
                raise ValidationError("the genesis schema may only create templates")
            return
        template = self._get_optional(schema_hash)
        if not template or template.tipo != "template":
            raise ValidationError("schema must reference an existing template")
        if template.corpo.get("alvo") != tipo:
            raise ValidationError(f"template targets {template.corpo.get('alvo')!r}, not {tipo!r}")
        validator = Draft202012Validator(template.corpo.get("json_schema", {}))
        errors = sorted(validator.iter_errors(corpo), key=lambda e: tuple(str(x) for x in e.path))
        if errors:
            err = errors[0]
            path = "/".join(map(str, err.absolute_path)) or "$"
            raise ValidationError(f"body {path}: {err.message}")

    def get(self, digest: str) -> ObjectRecord:
        record = self._get_optional(digest)
        if not record:
            raise NotFound(f"object not found: {digest}")
        if not record.verify():
            raise IntegrityError(f"content hash mismatch: {digest}")
        return record

    def _get_optional(self, digest: str) -> ObjectRecord | None:
        row = self.db.execute("SELECT * FROM objetos WHERE hash = ?", (digest,)).fetchone()
        if not row:
            return None
        return ObjectRecord(row["hash"], row["tipo"], row["schema_hash"], tuple(json.loads(row["pais_json"])), json.loads(row["corpo_json"]), row["criado_em"])

    def _require_hash(self, digest: str) -> None:
        if not _is_hash(digest):
            raise ValidationError("hash must be a lowercase sha256 hex digest")
        if not self._get_optional(digest):
            raise NotFound(f"object not found: {digest}")

    def append_event(self, digest: str, tipo: str, dados: dict[str, Any], *, at: str | None = None) -> int:
        with self.transaction():
            self._require_hash(digest)
            return self._event(digest, tipo, dados, at or _now())

    def _event(self, digest: str, tipo: str, dados: dict[str, Any], at: str) -> int:
        if not tipo or not tipo.strip():
            raise ValidationError("event type cannot be empty")
        payload = canonical_json(dados).decode()
        cursor = self.db.execute("INSERT INTO eventos(objeto_hash, tipo, dados_json, em) VALUES (?, ?, ?, ?)", (digest, tipo, payload, at))
        return int(cursor.lastrowid)

    def events(self, digest: str, *, after: int = 0) -> list[dict[str, Any]]:
        self._require_hash(digest)
        rows = self.db.execute("SELECT * FROM eventos WHERE objeto_hash = ? AND seq > ? ORDER BY seq", (digest, after))
        return [{"seq": r["seq"], "objeto": r["objeto_hash"], "tipo": r["tipo"], "dados": json.loads(r["dados_json"]), "em": r["em"]} for r in rows]

    def set_pointer(self, nome: str, digest: str, *, expected: str | None = None, at: str | None = None) -> None:
        """Atomically compare-and-swap a pointer and record old/new values."""
        if not nome or nome.startswith("/") or nome.endswith("/") or "//" in nome:
            raise ValidationError("invalid pointer name")
        timestamp = at or _now()
        with self.transaction():
            self._require_hash(digest)
            row = self.db.execute("SELECT hash FROM ponteiros WHERE nome = ?", (nome,)).fetchone()
            old = row[0] if row else None
            if expected is not None and old != expected:
                raise Conflict(f"pointer {nome!r} is {old!r}, expected {expected!r}")
            self.db.execute(
                "INSERT INTO ponteiros VALUES (?, ?, ?) ON CONFLICT(nome) DO UPDATE SET hash=excluded.hash, atualizado_em=excluded.atualizado_em",
                (nome, digest, timestamp),
            )
            self._event(digest, "ponteiro_movido", {"nome": nome, "de": old, "para": digest}, timestamp)

    def resolve_pointer(self, nome: str) -> ObjectRecord:
        row = self.db.execute("SELECT hash FROM ponteiros WHERE nome = ?", (nome,)).fetchone()
        if not row:
            raise NotFound(f"pointer not found: {nome}")
        return self.get(row[0])

    def lineage(self, digest: str, *, direction: str = "back") -> list[dict[str, str | int]]:
        self._require_hash(digest)
        if direction not in {"back", "forward"}:
            raise ValidationError("direction must be 'back' or 'forward'")
        join = "a.de_hash = walk.hash" if direction == "back" else "a.para_hash = walk.hash"
        select = "a.para_hash" if direction == "back" else "a.de_hash"
        query = f"""WITH RECURSIVE walk(hash, depth) AS (
          VALUES (?, 0) UNION SELECT {select}, walk.depth + 1 FROM arestas a JOIN walk ON {join}
        ) SELECT walk.hash, walk.depth, objetos.tipo FROM walk JOIN objetos USING(hash) ORDER BY depth, hash"""
        return [dict(r) for r in self.db.execute(query, (digest,))]

    def put_blob(self, data: bytes) -> str:
        if not isinstance(data, bytes):
            raise ValidationError("blob must be bytes")
        digest = sha256(data).hexdigest()
        destination = self.blobs / digest
        if not destination.exists():
            temporary = self.blobs / f".{digest}.tmp"
            temporary.write_bytes(data)
            temporary.replace(destination)
        elif destination.read_bytes() != data:
            raise IntegrityError(f"blob collision: {digest}")
        return digest

    def get_blob(self, digest: str) -> bytes:
        if not _is_hash(digest):
            raise ValidationError("blob hash must be a lowercase sha256 hex digest")
        path = self.blobs / digest
        if not path.is_file():
            raise NotFound(f"blob not found: {digest}")
        data = path.read_bytes()
        if sha256(data).hexdigest() != digest:
            raise IntegrityError(f"blob hash mismatch: {digest}")
        return data


def _is_hash(value: str) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
