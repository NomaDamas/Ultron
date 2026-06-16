"""Runtime configuration CLI: ``python -m ultron.config set|get|status``.

Secrets are never printed. ``set`` accepts non-secret values as a positional
argument and secret values via ``--stdin`` (no-echo prompt when interactive).
``get`` and ``status`` return redacted ``SecretRef`` views only.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from typing import Sequence

from ultron.config import ALL_KEYS, ConfigService, build_config_service, host_label as _host_label, is_secret_key


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ultron config", description="Ultron runtime configuration")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="print redacted model settings")

    get_parser = sub.add_parser("get", help="print a redacted config value")
    get_parser.add_argument("key", help="dotted config key, e.g. llm.model")

    set_parser = sub.add_parser("set", help="set a config value")
    set_parser.add_argument("key", help="dotted config key, e.g. llm.api_key")
    set_parser.add_argument("value", nargs="?", default=None, help="value for non-secret keys")
    set_parser.add_argument("--stdin", action="store_true", help="read a secret value from stdin (no echo)")
    return parser


def _read_secret_from_stdin(prompt: str = "value: ") -> str:
    if sys.stdin is not None and sys.stdin.isatty():
        return getpass.getpass(prompt)
    return sys.stdin.readline().rstrip("\n")


def main(argv: Sequence[str] | None = None, *, service: ConfigService | None = None, out=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    svc = service if service is not None else build_config_service()
    stream = out if out is not None else sys.stdout

    if args.command == "status":
        print(json.dumps(svc.model_settings_read().model_dump(mode="json"), indent=2, sort_keys=True), file=stream)
        return 0

    if args.command == "get":
        if args.key not in ALL_KEYS:
            print(f"unknown config key: {args.key}", file=sys.stderr)
            return 2
        if is_secret_key(args.key):
            print(json.dumps(svc.secret_ref(args.key).model_dump(mode="json"), indent=2, sort_keys=True), file=stream)
        elif args.key.endswith(".base_url"):
            # Base URLs can carry credentials/paths; expose only host label + source.
            value, source = svc.resolve(args.key)
            print(json.dumps({"key": args.key, "host": _host_label(value), "configured": bool(value), "source": source}, indent=2, sort_keys=True), file=stream)
        else:
            print(json.dumps({"key": args.key, "value": svc.value(args.key)}, indent=2, sort_keys=True), file=stream)
        return 0

    if args.command == "set":
        if args.key not in ALL_KEYS:
            print(f"unknown config key: {args.key}", file=sys.stderr)
            return 2
        if is_secret_key(args.key):
            if not args.stdin:
                print("secret keys must be set via --stdin (no-echo input)", file=sys.stderr)
                return 2
            value = _read_secret_from_stdin()
            if not value:
                print("no secret value provided", file=sys.stderr)
                return 2
        else:
            if args.value is None:
                print("a value is required for non-secret keys", file=sys.stderr)
                return 2
            value = args.value
        svc.set(args.key, value, actor="cli")
        # Never echo the value; confirm with a redacted view.
        if is_secret_key(args.key):
            print(json.dumps({"updated": args.key, "ref": svc.secret_ref(args.key).model_dump(mode="json")}, sort_keys=True), file=stream)
        else:
            print(json.dumps({"updated": args.key}, sort_keys=True), file=stream)
        return 0

    parser.print_help(file=stream)
    return 1


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    raise SystemExit(main())
