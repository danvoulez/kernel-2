from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .errors import EngineError
from .store import Store


def _json(path: str) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise argparse.ArgumentTypeError("JSON root must be an object")
    return value


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="engine")
    root.add_argument("--db", default="engine.db")
    commands = root.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="initialize a database")
    init.add_argument("--blobs")
    genesis = commands.add_parser("genesis-template")
    genesis.add_argument("body", type=_json)
    create = commands.add_parser("create")
    create.add_argument("tipo")
    create.add_argument("schema")
    create.add_argument("body", type=_json)
    create.add_argument("--parent", action="append", default=[])
    get = commands.add_parser("get")
    get.add_argument("hash")
    pointer = commands.add_parser("set-pointer")
    pointer.add_argument("name")
    pointer.add_argument("hash")
    pointer.add_argument("--expected")
    resolve = commands.add_parser("resolve")
    resolve.add_argument("name")
    lineage = commands.add_parser("lineage")
    lineage.add_argument("hash")
    lineage.add_argument("--direction", choices=["back", "forward"], default="back")
    commands.add_parser("audit", help="verify all hashes, references, edges, events and SQLite pages")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        with Store(args.db, getattr(args, "blobs", None)) as store:
            if args.command == "init":
                result: Any = {"database": args.db, "blobs": str(store.blobs)}
            elif args.command == "genesis-template":
                record = store.create_genesis_template(args.body)
                result = {"hash": record.hash, "tipo": record.tipo, "schema": record.schema, "pais": record.pais, "corpo": record.corpo, "criado_em": record.criado_em}
            elif args.command == "create":
                record = store.create(args.tipo, args.schema, args.body, args.parent)
                result = {"hash": record.hash, "tipo": record.tipo, "schema": record.schema, "pais": record.pais, "corpo": record.corpo, "criado_em": record.criado_em}
            elif args.command == "get":
                record = store.get(args.hash)
                result = {"hash": record.hash, "tipo": record.tipo, "schema": record.schema, "pais": record.pais, "corpo": record.corpo, "criado_em": record.criado_em}
            elif args.command == "set-pointer":
                store.set_pointer(args.name, args.hash, expected=args.expected)
                result = {"name": args.name, "hash": args.hash}
            elif args.command == "resolve":
                record = store.resolve_pointer(args.name)
                result = {"name": args.name, "hash": record.hash}
            elif args.command == "lineage":
                result = store.lineage(args.hash, direction=args.direction)
            else:
                result = store.audit()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except EngineError as exc:
        root = parser()
        root.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
