"""ForkMark command-line interface.

The SDK's primary interface is the Python API (``import forkmark``); see the
package README. This CLI is a thin stub retained for the ``forkmark`` entry point.
"""
from __future__ import annotations

import argparse
from typing import List, Optional


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forkmark",
        description="ForkMark command-line interface. Use the Python SDK (import forkmark) "
                    "to log model comparisons for validation.",
    )
    parser.add_subparsers(dest="command")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
