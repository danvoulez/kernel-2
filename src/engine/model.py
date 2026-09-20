from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any, Mapping, Sequence

from .errors import ValidationError


ObjectBody = Mapping[str, Any]


def canonical_json(value: Any) -> bytes:
    """Return the sole byte representation accepted for hashing and persistence."""
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"value is not canonical JSON: {exc}") from exc
    return text.encode("utf-8")


def content_hash(*, tipo: str, schema: str, pais: Sequence[str], corpo: ObjectBody) -> str:
    return sha256(canonical_json({"tipo": tipo, "schema": schema, "pais": list(pais), "corpo": corpo})).hexdigest()


@dataclass(frozen=True, slots=True)
class ObjectRecord:
    hash: str
    tipo: str
    schema: str
    pais: tuple[str, ...]
    corpo: dict[str, Any]
    criado_em: str

    def verify(self) -> bool:
        return self.hash == content_hash(tipo=self.tipo, schema=self.schema, pais=self.pais, corpo=self.corpo)

