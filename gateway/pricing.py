"""DeepSeek 官方峰谷定价(2026-08-19 起生效, 元/百万 tokens)。

官方规则(https://api-docs.deepseek.com/zh-cn/quick_start/pricing/):
- 高峰时段: 北京时间 9:00-12:00、14:00-18:00(含边界, 临界按波峰); 其余为空闲时段
- 空闲时段价格为高峰时段价格的一半
- 缓存命中价 = 未命中价 / 30(官方表 0.05/1.5、0.10/3.0 ...)

说明:
- 价格可能变动, 以官方页面为准; 显式配置(cost_per_1k_*)优先于本表
- deepseek-chat 未在官方页列出(2026-08-19 起页面仅 deepseek-v4-flash/pro),
  按 v4-flash 价格估算; 新配置建议改用 deepseek-v4-flash
- 可开启 dynamic_pricing 自动从官网拉取最新价格; 拉取/解析失败时回退到磁盘缓存或内置价表
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import time
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# DeepSeek 官方定价页面(HTML 表格, 不是 JSON API)
PRICING_URL = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
# 拉取一次后在本进程内缓存, 避免每次 LLM 调用都打官网
PRICING_CACHE_TTL_SECONDS = 6 * 60 * 60

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

# url -> (fetch_time, pricing)
_pricing_cache: dict[str, tuple[float, dict[str, dict[str, float]]]] = {}


def _resolve_ttl(ttl: float | None) -> float:
    """未显式传 TTL 时读取环境变量 AI_GATEWAY_PRICING_TTL_SECONDS。"""
    if ttl is not None:
        return ttl
    raw = os.environ.get("AI_GATEWAY_PRICING_TTL_SECONDS")
    if raw:
        try:
            return float(raw)
        except ValueError:
            logger.warning("AI_GATEWAY_PRICING_TTL_SECONDS 不是合法数字: %r", raw)
    return PRICING_CACHE_TTL_SECONDS


def _pricing_cache_file() -> Path:
    """磁盘缓存路径; 可用 AI_GATEWAY_PRICING_CACHE_FILE 覆盖, 默认 ~/.cache/ai-tools/..."""
    raw = os.environ.get("AI_GATEWAY_PRICING_CACHE_FILE")
    if raw:
        return Path(raw)
    return Path.home() / ".cache" / "ai-tools" / "deepseek-pricing.json"


def _read_disk_pricing(url: str) -> dict[str, dict[str, float]] | None:
    try:
        data = json.loads(_pricing_cache_file().read_text(encoding="utf-8"))
        if data.get("url") == url and isinstance(data.get("pricing"), dict):
            return data["pricing"]
    except Exception:
        return None
    return None


def _write_disk_pricing(url: str, pricing: dict[str, dict[str, float]]) -> None:
    try:
        path = _pricing_cache_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"url": url, "fetched_at": time.time(), "pricing": pricing}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        logger.debug("写入 DeepSeek 定价磁盘缓存失败", exc_info=True)


class _TableParser(HTMLParser):
    """轻量 HTML table 解析器: 只提取所有 <tr> 的文本单元格。"""

    def __init__(self) -> None:
        super().__init__()
        self._table_depth = 0
        self._in_row = False
        self._in_cell = False
        self._cell_parts: list[str] = []
        self._row: list[str] = []
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._table_depth += 1
        elif tag == "tr" and self._table_depth:
            self._in_row = True
            self._row = []
        elif tag in ("td", "th") and self._table_depth:
            self._in_cell = True
            self._cell_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self._table_depth:
            self._table_depth -= 1
        elif tag == "tr" and self._in_row:
            self.rows.append(self._row)
            self._in_row = False
        elif tag in ("td", "th") and self._in_cell:
            self._row.append(" ".join("".join(self._cell_parts).split()))
            self._in_cell = False

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell_parts.append(data)


def _extract_table_rows(html: str) -> list[list[str]]:
    parser = _TableParser()
    parser.feed(html)
    return parser.rows


def parse_pricing_html(html: str) -> dict[str, dict[str, float]]:
    """从 DeepSeek 官网 HTML 页面解析价格表, 返回与 DEFAULT_PRICING 同构的 dict。

    当前官网是 Docusaurus 静态页, 价格在“模型细节”表格中:
    - 表头: 模型 | deepseek-v4-flash | deepseek-v4-pro | ...
    - 价格行: 缓存命中/未命中/输出 × 空闲/高峰
    """
    rows = _extract_table_rows(html)
    header = next((r for r in rows if r and r[0] == "模型"), None)
    if header is None or len(header) < 2:
        raise ValueError("未在 DeepSeek 官网页面中找到价格表头")

    models = header[1:]
    result: dict[str, dict[str, float]] = {m: {} for m in models}
    metric: str | None = None

    for row in rows:
        text = " ".join(row)
        if "缓存命中" in text and "输入" in text:
            metric = "input_hit"
        elif "缓存未命中" in text and "输入" in text:
            metric = "input_miss"
        elif "百万tokens输出" in text:
            metric = "output"

        if metric is None:
            continue
        if any("空闲" in c for c in row):
            period = "offpeak"
        elif any("高峰" in c for c in row):
            period = "peak"
        else:
            continue

        prices = row[-len(models):]
        if len(prices) != len(models):
            continue
        for model, cell in zip(models, prices):
            m = re.search(r"(\d+(?:\.\d+)?)", cell or "")
            if not m:
                continue
            result[model][f"{metric}_{period}"] = float(m.group(1))

    required = {
        "input_hit_peak",
        "input_hit_offpeak",
        "input_miss_peak",
        "input_miss_offpeak",
        "output_peak",
        "output_offpeak",
    }
    for model in models:
        if not required.issubset(result[model]):
            raise ValueError(f"DeepSeek 价格表解析不完整: {model} -> {result[model]}")
    return result


def fetch_deepseek_pricing(
    url: str = PRICING_URL,
    timeout: float = 5.0,
) -> dict[str, dict[str, float]]:
    """请求官网并解析最新定价。失败/解析失败会抛出异常, 由调用方决定回退。"""
    req = urllib.request.Request(url, headers={"User-Agent": "ai-tools-pricing/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        html = resp.read().decode("utf-8", "ignore")
    return parse_pricing_html(html)


def get_deepseek_pricing(
    url: str = PRICING_URL,
    *,
    ttl: float | None = None,
) -> dict[str, dict[str, float]]:
    """获取官网最新价格。

    优先级/回退:
    1. 进程内未过期的缓存;
    2. 官网实时拉取成功(并写磁盘缓存);
    3. 官网失败时使用磁盘上的最近一次成功价格;
    4. 都没有时抛出异常, 由上层回退内置价表。
    """
    ttl = _resolve_ttl(ttl)
    now = time.time()
    cached = _pricing_cache.get(url)
    if cached and now - cached[0] < ttl:
        return cached[1]

    try:
        pricing = fetch_deepseek_pricing(url)
    except Exception:
        disk = _read_disk_pricing(url)
        if disk is not None:
            logger.warning("DeepSeek 官网定价获取失败, 使用磁盘缓存")
            _pricing_cache[url] = (now, disk)
            return disk
        raise

    _pricing_cache[url] = (now, pricing)
    _write_disk_pricing(url, pricing)
    return pricing


def lookup_dynamic_pricing(
    model: str | None,
    url: str = PRICING_URL,
    *,
    ttl: float | None = None,
) -> dict[str, float] | None:
    """从官网动态价格中查单个模型; 支持 'provider/model' 形式取后半段。"""
    if not model:
        return None
    pricing = get_deepseek_pricing(url, ttl=ttl)
    return pricing.get(model.split("/")[-1].strip())


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
