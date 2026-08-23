"""ai-tools-cli 骨架测试。"""

from __future__ import annotations

from ai_tools_cli.main import build_parser


def test_parser_has_review_subcommand():
    parser = build_parser()
    args = parser.parse_args(["review", "--repo", "."])
    assert args.command == "review"
    assert args.repo == "."


def test_parser_has_pricing_subcommand():
    parser = build_parser()
    args = parser.parse_args(["pricing", "show"])
    assert args.command == "pricing"
    assert args.pricing_command == "show"
