"""ai-tools-cli pricing 子命令: 查看/刷新 DeepSeek 定价。"""

from __future__ import annotations

import argparse

from gateway.pricing import (
    _pricing_cache,
    get_deepseek_pricing,
)


def _print_pricing(data: dict[str, dict[str, float]]) -> None:
    print(f"{'模型':<28} {'输入命中(峰/闲)':<20} {'输入未命中(峰/闲)':<22} {'输出(峰/闲)':<18}")
    for model, prices in data.items():
        print(
            f"{model:<28} "
            f"{prices['input_hit_peak']:.2f}/{prices['input_hit_offpeak']:.2f}          "
            f"{prices['input_miss_peak']:.2f}/{prices['input_miss_offpeak']:.2f}          "
            f"{prices['output_peak']:.2f}/{prices['output_offpeak']:.2f}"
        )


def _run_show(args: argparse.Namespace) -> int:
    data = get_deepseek_pricing()
    _print_pricing(data)
    return 0


def _run_refresh(args: argparse.Namespace) -> int:
    # 清掉进程内缓存, 强制重新请求官网并刷新磁盘缓存
    _pricing_cache.clear()
    data = get_deepseek_pricing()
    _print_pricing(data)
    return 0


def add_pricing_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "pricing",
        help="Show or refresh DeepSeek pricing",
        description="查看/刷新 DeepSeek 官方峰谷定价",
    )
    sub = parser.add_subparsers(dest="pricing_command", required=True)

    show = sub.add_parser("show", help="show current DeepSeek pricing")
    show.set_defaults(func=_run_show)

    refresh = sub.add_parser("refresh", help="force refresh from official site")
    refresh.set_defaults(func=_run_refresh)
