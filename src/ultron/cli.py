"""Console entrypoint: ``ultron config set|get|status`` dispatches to ``ultron.config``."""

from __future__ import annotations

import sys
from typing import Sequence

from ultron.config.__main__ import main as config_main


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "config":
        return config_main(args[1:])
    if not args:
        print("usage: ultron config <status|get|set> ...", file=sys.stderr)
        return 1
    print(f"unknown ultron command: {args[0]}", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
