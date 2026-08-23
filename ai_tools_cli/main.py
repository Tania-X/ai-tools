"""ai-tools-cli 统一入口。

用法:
    ai-tools-cli review --repo . --base main --head HEAD
    ai-tools-cli pricing show
    ai-tools-cli pricing refresh
"""

from __future__ import annotations

import argparse

from .commands.pricing import add_pricing_parser
from .commands.review import add_review_parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-tools-cli",
        description="AI × DevOps 工具集命令行入口",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    add_review_parser(sub)
    add_pricing_parser(sub)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
