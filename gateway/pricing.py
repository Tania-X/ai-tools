"""DeepSeek 官方峰谷定价(2026-08-19 起生效, 元/百万 tokens)。

官方规则(https://api-docs.deepseek.com/zh-cn/quick_start/pricing/):
- 高峰时段: 北京时间 9:00-12:00、14:00-18:00(含边界, 临界按波峰); 其余为空闲时段
- 空闲时段价格为高峰时段价格的一半
- 缓存命中价 = 未命中价 / 30(官方表 0.05/1.5、0.10/3.0 ...)

说明:
- 价格可能变动, 以官方页面为准; 显式配置(cost_per_1k_*)优先于本表
- deepseek-chat 未在官方页列出(2026-08-19 起页面仅 deepseek-v4-flash/pro),
  按 v4-flash 价格估算; 新配置建议改用 deepseek-v4-flash
"""

from __future__ import annotations

import datetime
from typing import Any

DEFAULT_PRICING: dict[str, dict[str, float]] = {
    "deepseek-v4-flash": {
        "input_hit_peak": 0.10,
        "input_hit_offpeak": 0.05,
        "input_miss_peak": 3.0,
        "input_miss_offpeak": 1.5,
        "output_peak": 9.0,
        "output_offpeak": 4.5,
    },
    "deepseek-v4-pro": {
        "input_hit_peak": 0.30,
        "input_hit_offpeak": 0.15,
        "input_miss_peak": 9.0,
        "input_miss_offpeak": 4.5,
        "output_peak": 27.0,
        "output_offpeak": 13.5,
    },
    "deepseek-chat": {  # 未列官方价, 按 v4-flash 估算
        "input_hit_peak": 0.10,
        "input_hit_offpeak": 0.05,
        "input_miss_peak": 3.0,
        "input_miss_offpeak": 1.5,
        "output_peak": 9.0,
        "output_offpeak": 4.5,
    },
}


def is_peak_hour(now: datetime.datetime | None = None) -> bool:
    """当前(北京时间 UTC+8)是否高峰时段。

    高峰: 9:00-12:00、14:00-18:00, 含边界(临界按波峰, 即 12:00/18:00 整点仍算高峰)。
    """
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    beijing_hour = (now + datetime.timedelta(hours=8)).hour
    return (9 <= beijing_hour <= 12) or (14 <= beijing_hour <= 18)


def lookup_pricing(model: str | None) -> dict[str, float] | None:
    """按模型名查内置价表; 支持 "provider/model" 形式取后半段。"""
    if not model:
        return None
    name = model.split("/")[-1].strip()
    return DEFAULT_PRICING.get(name)


def compute_cost(
    usage: dict[str, Any],
    pricing: dict[str, float] | None,
    *,
    now: datetime.datetime | None = None,
) -> float | None:
    """按峰谷 + 缓存命中/未命中计算成本(元)。

    usage 需含 prompt_cache_hit_tokens / prompt_cache_miss_tokens(DeepSeek 返回);
    缺失时全部按未命中计(偏保守)。返回 None 表示无价表可用。
    """
    if not pricing:
        return None
    hit = int(usage.get("prompt_cache_hit_tokens", 0) or 0)
    miss = int(usage.get("prompt_cache_miss_tokens", 0) or 0)
    if hit + miss == 0:
        miss = int(usage.get("prompt_tokens", 0) or 0)
    out_t = int(usage.get("completion_tokens", 0) or 0)
    key = "peak" if is_peak_hour(now) else "offpeak"
    cost = (
        hit / 1e6 * pricing[f"input_hit_{key}"]
        + miss / 1e6 * pricing[f"input_miss_{key}"]
        + out_t / 1e6 * pricing[f"output_{key}"]
    )
    return round(cost, 6)
